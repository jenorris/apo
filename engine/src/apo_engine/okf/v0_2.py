"""OKF v0.2 reader — trust, provenance, and lifecycle families.

v0.2 adds nested frontmatter that the scalar-only parser cannot see, so this
module reads the structured mapping (:func:`..frontmatter.parse_mapping`).

Families (all optional; SPEC §11 forbids rejecting a concept that omits them):

* **trust** — ``generated: {by, at}``, ``verified: [{by, at}]``
* **provenance** — ``sources: [{resource, …}]``, ``usage_window: {from, to}``
* **lifecycle** — ``status``, ``stale_after``
"""

from __future__ import annotations

from typing import Any

from apo_engine.okf.model import (
    Attribution,
    ConceptMeta,
    SourceRef,
    UsageWindow,
    _as_text,
    as_list,
)

VERSION = "0.2"

STATUS_VALUES = ("draft", "stable", "deprecated")
DEFAULT_STATUS = "stable"

#: Keys that only exist in v0.2.
MARKER_KEYS = ("generated", "verified", "sources", "usage_window", "stale_after")


def parse(
    frontmatter: dict[str, Any],
    body: str = "",
    *,
    meta: ConceptMeta | None = None,
) -> ConceptMeta:
    """Fill v0.2 fields on ``meta`` (created when omitted). Never overwrites."""
    meta = meta or ConceptMeta()

    if meta.generated is None:
        meta.generated = Attribution.from_raw(frontmatter.get("generated"))

    if not meta.verified:
        # SPEC §11: a bare mapping must be read as a one-element list.
        entries = [Attribution.from_raw(e) for e in as_list(frontmatter.get("verified"))]
        meta.verified = [e for e in entries if e is not None]

    if not meta.sources:
        refs = [SourceRef.from_raw(s) for s in as_list(frontmatter.get("sources"))]
        meta.sources = [r for r in refs if r is not None]

    if meta.usage_window is None:
        meta.usage_window = UsageWindow.from_raw(frontmatter.get("usage_window"))
        # A window declared at concept level frames every source that lacks one.
        if meta.usage_window is not None:
            for ref in meta.sources:
                if ref.usage_window is None:
                    ref.usage_window = meta.usage_window

    if meta.status is None:
        meta.status = _as_text(frontmatter.get("status"))

    if meta.stale_after is None:
        meta.stale_after = _as_text(frontmatter.get("stale_after"))

    return meta


def detect(frontmatter: dict[str, Any], body: str = "") -> bool:
    """True when the note carries any v0.2-only key."""
    return any(frontmatter.get(k) is not None for k in MARKER_KEYS)


def effective_status(meta: ConceptMeta) -> str:
    """``status`` with the spec default applied.

    Unknown values are returned as-is — SPEC §11 forbids rejecting a concept
    for an unrecognized value, so a consumer surfaces it rather than dropping it.
    """
    return (meta.status or DEFAULT_STATUS).strip() or DEFAULT_STATUS
