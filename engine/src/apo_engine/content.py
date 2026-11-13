"""Content identity and frontmatter parsing."""

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

def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)

def body_start_line(text: str) -> tuple[str, int]:
    """Return body text and its one-based first line."""
    match = _FRONTMATTER.match(text)
    if not match:
        return text, 1
    frontmatter = match.group(0)
    return text[len(frontmatter) :], frontmatter.count("\n") + 1

def content_hash(text: str) -> str:
    return hashlib.blake2b(
        text.encode("utf-8", "replace"), digest_size=8
    ).hexdigest()

def file_hash(text: str) -> str:
    return hashlib.blake2b(
        text.encode("utf-8", "replace"), digest_size=32
    ).hexdigest()

def compile_ignore(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(fnmatch.translate(pattern)) for pattern in patterns]

def is_ignored(path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.fullmatch(path) is not None for pattern in patterns)


def escape_like(value: str) -> str:
    """Escape SQLite LIKE wildcard characters in a literal path."""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_FRONTMATTER_YAML = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")
_FM_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
def _construct_yaml_timestamp(loader: yaml.SafeLoader, node: yaml.Node):
    """Parse YAML timestamps; keep invalid date-like scalars as strings.

    PyYAML matches ``YYYY-MM-DD`` (and friends) as timestamps before validating
    month/day. Bad vault values like ``2017-00-00`` then raise ``ValueError``
    from ``datetime.date`` — which is *not* a ``YAMLError``, so a bare
    ``safe_load`` can crash the watcher. Fall back to the original scalar.
    """
    try:
        return yaml.constructor.SafeConstructor.construct_yaml_timestamp(loader, node)
    except (ValueError, OverflowError):
        return loader.construct_scalar(node)


class _FrontmatterLoader(yaml.SafeLoader):
    """SafeLoader that tolerates invalid YAML 1.1 timestamp scalars."""


_FrontmatterLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    _construct_yaml_timestamp,
)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_YAML.match(text)
    if not m:
        return {}
    try:
        data = yaml.load(m.group(1), Loader=_FrontmatterLoader)
    except (yaml.YAMLError, ValueError, OverflowError):
        return {}
    return data if isinstance(data, dict) else {}


def note_frontmatter(text: str, path: str | Path | None = None) -> dict | None:
    """Catalog fields for a note.

    Markdown: fenced YAML frontmatter, or ``None`` when absent.
    YAML note (``path`` ends in ``.yaml``/``.yml``): whole-file mapping.
    """
    if path is not None:
        from apo_engine.note_format import is_yaml_note, parse_yaml_document

        if is_yaml_note(path):
            return parse_yaml_document(text)
    if not _FRONTMATTER_YAML.match(text):
        return None
    return _parse_frontmatter(text)


def note_catalog_json(text: str, rel: str, mtime: float | None = None) -> str | None:
    """JSON for ``files.frontmatter``. Empty mapping stores ``{}`` (not NULL)."""
    from apo_engine.note_format import is_mmd_note, is_yaml_note, parse_yaml_document

    if is_mmd_note(rel):
        from apo_engine.mermaid_index import merge_frontmatter_for_mmd

        return json.dumps(merge_frontmatter_for_mmd(text, rel, mtime=mtime), default=str)
    if is_yaml_note(rel):
        fm = parse_yaml_document(text)
        if fm is None:
            return None
        return json.dumps(fm, default=str)
    if not _FRONTMATTER_YAML.match(text):
        return None
    fm = _parse_frontmatter(text)
    # Include empty fenced frontmatter so filter_notes can see the note.
    return json.dumps(fm, default=str)


def _known_vault_ids() -> frozenset[str] | None:
    """Vault ids for ``[[vault:path]]`` detection — None accepts any well-formed prefix.

    Deliberately does not load the registry: indexing runs per bound vault and a
    registry load here would warm ``vaults.load_bindings``' cache from inside it.
    """
    return None

__owned__ = ['strip_frontmatter', 'body_start_line', 'content_hash', 'file_hash', 'compile_ignore', 'is_ignored', 'escape_like', 'note_catalog_json', '_FRONTMATTER', '_FRONTMATTER_YAML', '_HEADING', '_FM_KEY_SAFE', '_construct_yaml_timestamp', '_FrontmatterLoader', '_parse_frontmatter', 'note_frontmatter', '_known_vault_ids']
