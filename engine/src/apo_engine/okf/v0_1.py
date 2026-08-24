"""OKF v0.1 reader.

v0.1 carries provenance in two places that v0.2 supersedes (SPEC §13.1):

* ``timestamp`` — a bare scalar, superseded by ``generated: {by, at}``
* a ``# Citations`` body list — superseded by the ``sources`` frontmatter key

This module reads only those v0.1 shapes. :func:`apo_engine.okf.read_concept`
layers it under the v0.2 reader so both are always consulted.
"""

from __future__ import annotations

import re
from typing import Any

from apo_engine.okf.model import ConceptMeta, _as_text

VERSION = "0.1"

# A `# Citations` / `## Citations` section, up to the next heading of the
# same-or-higher level or end of body.
_CITATIONS_RE = re.compile(
    r"(?ms)^(?P<hashes>#{1,6})\s*Citations\s*$\n(?P<body>.*?)(?=^#{1,6}\s|\Z)"
)
_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)\s+(.*\S)\s*$")
_MD_LINK_RE = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<href>[^)]+)\)")
_WIKI_LINK_RE = re.compile(r"\[\[(?P<target>[^\]|]+)(?:\|[^\]]*)?\]\]")


def parse_citations(body: str) -> list[str]:
    """Extract resources from a v0.1 ``# Citations`` body list.

    Each list item yields one resource: the URL of a markdown link, the target
    of a wiki-link, or the raw item text.
    """
    match = _CITATIONS_RE.search(body or "")
    if not match:
        return []
    out: list[str] = []
    for item in _LIST_ITEM_RE.findall(match.group("body")):
        text = item.strip()
        if not text:
            continue
        link = _MD_LINK_RE.search(text)
        if link:
            out.append(link.group("href").strip())
            continue
        wiki = _WIKI_LINK_RE.search(text)
        if wiki:
            out.append(wiki.group("target").strip())
            continue
        out.append(text)
    return out


def parse(
    frontmatter: dict[str, Any],
    body: str = "",
    *,
    meta: ConceptMeta | None = None,
) -> ConceptMeta:
    """Fill v0.1 fields on ``meta`` (created when omitted). Never overwrites."""
    meta = meta or ConceptMeta()

    if meta.legacy_timestamp is None:
        # Apo has always accepted these as "when did this change" equivalents.
        for key in ("timestamp", "updated", "ingested_at", "date"):
            val = _as_text(frontmatter.get(key))
            if val:
                meta.legacy_timestamp = val
                break

    if not meta.legacy_citations:
        meta.legacy_citations = parse_citations(body)

    return meta


def detect(frontmatter: dict[str, Any], body: str = "") -> bool:
    """True when the note carries a v0.1-only provenance shape."""
    if any(frontmatter.get(k) for k in ("timestamp", "updated", "ingested_at", "date")):
        return True
    return bool(_CITATIONS_RE.search(body or ""))
