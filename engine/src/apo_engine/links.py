"""Typed link extraction — the one parser behind the ``backlinks`` table.

A note links out three ways:

* ``[[wikilinks]]`` in the body (alias and heading kept),
* relative markdown links ``[text](path.md#heading)`` in the body,
* frontmatter fields the OKF contract declares as link fields (``related``,
  ``supersedes``, ``sources.resource``, ``resource`` by default).

Extraction is pure: it returns :class:`Link` rows and never touches the index.
Resolution to a real vault path happens later, set-based, in
``core.resolve_links`` — so a target created after its source still resolves.

Keys are ASCII-lowercased (:func:`link_key`) to match SQLite's ``lower()``,
which the ``files.link_*`` generated columns use on the other side of the join.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote

from apo_engine import fm_path

# Bump when extraction semantics change; ``core`` re-extracts every indexed note
# (links only, no re-embed) when the stored version differs.
LINKS_SCHEMA_VERSION = "1"

# Default link fields when the vault's OKF contract declares none (or has no contract).
DEFAULT_LINK_FIELDS: tuple[tuple[str, str], ...] = (
    ("related", "related"),
    ("supersedes", "supersedes"),
    ("sources.resource", "source"),
    ("resource", "resource"),
)

# Per-source cap — the v0.28.1 runaway class (see core.BACKLINKS_PER_FILE_WARN).
MAX_LINKS_PER_FILE = 2000

NOTE_SUFFIXES = (".md", ".yaml", ".yml", ".mmd")

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_FRONTMATTER_OPEN = re.compile(r"^---\n")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_WIKILINK = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
# [text](href "title") / [text](<href with spaces>) — images (![..](..)) are skipped.
_MD_LINK = re.compile(r"(?<![!\]\\])\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# vault:path — no space or slash after the colon, so "[[Note: title]]" stays a name.
_VAULT_PREFIX = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63}):(?![/\s])(.+)$")
_KIND_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# ![[embed]] targets that are attachments, not notes.
_MEDIA_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf",
    ".mp3", ".mp4", ".mov", ".webm", ".wav", ".m4a", ".canvas", ".excalidraw",
)


def link_key(value: str) -> str:
    """ASCII-lowercase (SQLite ``lower()`` parity), strip one ``.md`` suffix."""
    out = value.strip().translate(_ASCII_LOWER)
    return out[:-3] if out.endswith(".md") else out


@dataclass(frozen=True)
class Link:
    kind: str  # wikilink | md_link | <contract kind>
    target_key: str  # link_key() of a vault path or name; raw value when external
    mode: str  # path (exact vault path) | name (wikilink-style stem/title/path)
    line: int
    text: str
    status: str = "unresolved"  # unresolved | external | foreign
    alias: str | None = None
    heading: str | None = None
    target_vault: str | None = None

    @property
    def target_stem(self) -> str:
        return self.target_key.rsplit("/", 1)[-1]


def is_kind_slug(kind: str) -> bool:
    return bool(_KIND_SLUG.match(kind))


def extract_links(
    rel: str,
    text: str,
    frontmatter: dict | None,
    *,
    link_fields: Iterable[tuple[str, str]] = DEFAULT_LINK_FIELDS,
    body: bool = True,
    known_vaults: frozenset[str] | None = None,
) -> list[Link]:
    """All outbound links of one note. ``body=False`` for YAML notes (fields only)."""
    src_dir = posixpath.dirname(rel.replace("\\", "/"))
    out: list[Link] = []
    fm_end = 0
    if body:
        lines = text.splitlines()
        fm_end = _frontmatter_end(lines)
        out.extend(_body_links(lines, fm_end, src_dir, known_vaults))
    if frontmatter:
        out.extend(_field_links(text, frontmatter, link_fields, src_dir, known_vaults))
    return out[:MAX_LINKS_PER_FILE]


def _frontmatter_end(lines: list[str]) -> int:
    """Index of the first body line (0 when there is no fenced frontmatter)."""
    if not lines or lines[0] != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return i + 1
    return 0


def _body_links(
    lines: list[str], start: int, src_dir: str, known_vaults: frozenset[str] | None
) -> Iterable[Link]:
    fence: str | None = None
    for idx in range(start, len(lines)):
        line = lines[idx]
        m = _FENCE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        if "[" not in line:
            continue
        scan = _INLINE_CODE.sub(lambda c: " " * len(c.group(0)), line) if "`" in line else line
        snippet = line.strip()[:200]
        lineno = idx + 1
        for w in _WIKILINK.finditer(scan):
            link = _wikilink(w.group(1), "wikilink", lineno, snippet, known_vaults)
            if link is not None:
                yield link
        for md in _MD_LINK.finditer(scan):
            href = md.group(1)
            if href.startswith("<") and href.endswith(">"):
                href = href[1:-1]
            link = _href_link(href, "md_link", lineno, snippet, src_dir, body=True)
            if link is not None:
                yield link


def _wikilink(
    inner: str, kind: str, line: int, text: str, known_vaults: frozenset[str] | None
) -> Link | None:
    target, _, alias = inner.partition("|")
    target, _, heading = target.partition("#")
    target = target.strip()
    if not target:
        return None  # [[#heading]] — self-anchor
    if target.lower().endswith(_MEDIA_SUFFIXES):
        return None
    alias = alias.strip() or None
    heading = heading.strip() or None
    vm = _VAULT_PREFIX.match(target)
    if vm and (known_vaults is None or vm.group(1).lower() in known_vaults):
        return Link(
            kind=kind,
            target_key=link_key(vm.group(2)),
            mode="name",
            line=line,
            text=text,
            status="foreign",
            alias=alias,
            heading=heading,
            target_vault=vm.group(1).lower(),
        )
    return Link(kind=kind, target_key=link_key(target), mode="name", line=line, text=text,
                alias=alias, heading=heading)


def _href_link(
    href: str, kind: str, line: int, text: str, src_dir: str, *, body: bool
) -> Link | None:
    """Body links must point at a note file; URLs only count from link fields."""
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    if href.startswith("//") or _SCHEME.match(href):
        if not body and href.lower().startswith(("http://", "https://")):
            return Link(kind=kind, target_key=href, mode="path", line=line, text=text, status="external")
        return None  # mailto:, obsidian://, data: …
    path, _, heading = href.partition("#")
    path = unquote(path.split("?", 1)[0]).strip()
    if not path:
        return None
    if body and not path.lower().endswith(NOTE_SUFFIXES):
        return None  # images, PDFs, directories
    joined = path.lstrip("/") if path.startswith("/") else posixpath.join(src_dir, path)
    norm = posixpath.normpath(joined)
    heading = unquote(heading).strip() or None
    if norm == ".." or norm.startswith("../"):
        return Link(kind=kind, target_key=norm, mode="path", line=line, text=text,
                    status="external", heading=heading)
    return Link(kind=kind, target_key=link_key(norm), mode="path", line=line, text=text, heading=heading)


def _field_links(
    text: str,
    frontmatter: dict,
    link_fields: Iterable[tuple[str, str]],
    src_dir: str,
    known_vaults: frozenset[str] | None,
) -> Iterable[Link]:
    for field, kind in link_fields:
        values = _flatten(fm_path.resolve_values(frontmatter, field))
        if not values:
            continue
        line = _field_line(text, field)
        for raw in values:
            value = _coerce(raw)
            if not value:
                continue
            snippet = f"{field}: {value}"[:200]
            if value.startswith("[[") and value.endswith("]]"):
                link = _wikilink(value[2:-2], kind, line, snippet, known_vaults)
            elif (
                value.startswith(("./", "../", "/", "//"))
                or _SCHEME.match(value)
                or value.lower().endswith(NOTE_SUFFIXES)
            ):
                link = _href_link(value, kind, line, snippet, src_dir, body=False)
            else:
                link = _wikilink(value, kind, line, snippet, known_vaults)
            if link is not None:
                yield link


def _flatten(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    for v in values:
        if isinstance(v, list):
            out.extend(_flatten(v))
        else:
            out.append(v)
    return out


def _coerce(raw: Any) -> str | None:
    """Link field value → candidate string. Dates, numbers and booleans are not links."""
    if isinstance(raw, dict):
        for key in ("path", "href", "resource", "target", "url"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    return None


def _field_line(text: str, field: str) -> int:
    """1-based line of the field's top-level key inside fenced frontmatter (1 if not found)."""
    top = field.split(".", 1)[0].split("[", 1)[0]
    if not _FRONTMATTER_OPEN.match(text):
        # YAML note — the whole file is the mapping.
        pat = re.compile(rf"^{re.escape(top)}\s*:")
        for i, line in enumerate(text.splitlines(), 1):
            if pat.match(line):
                return i
        return 1
    pat = re.compile(rf"^{re.escape(top)}\s*:")
    for i, line in enumerate(text.splitlines()[1:], 2):
        if line == "---":
            break
        if pat.match(line):
            return i
    return 1
