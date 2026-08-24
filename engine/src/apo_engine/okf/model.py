"""Version-agnostic view of an OKF concept.

Both :mod:`apo_engine.okf.v0_1` and :mod:`apo_engine.okf.v0_2` produce a
:class:`ConceptMeta`, so callers read one shape regardless of which spec
version a note was written against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

# SPEC §7 actor convention.
ACTOR_HUMAN = "human"
ACTOR_PROCESS = "process"
ACTOR_AGENT = "agent"


@dataclass(frozen=True)
class Actor:
    """An identity recorded in a trust field.

    SPEC §7 standardizes three forms: ``human:<id>``, ``process:<id>``, and
    ``<producer>/<version>`` for agents and tools. Trust classification keys
    off the ``human:`` prefix.
    """

    raw: str
    kind: str = ACTOR_AGENT

    @property
    def is_human(self) -> bool:
        return self.kind == ACTOR_HUMAN

    @property
    def ident(self) -> str:
        if self.kind in (ACTOR_HUMAN, ACTOR_PROCESS):
            return self.raw.split(":", 1)[1]
        return self.raw

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


def parse_actor(raw: Any) -> Actor | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith(f"{ACTOR_HUMAN}:"):
        return Actor(text, ACTOR_HUMAN)
    if text.startswith(f"{ACTOR_PROCESS}:"):
        return Actor(text, ACTOR_PROCESS)
    return Actor(text, ACTOR_AGENT)


@dataclass
class UsageWindow:
    """``{from, to}`` date range framing every ``usage_count`` beneath it."""

    start: str | None = None
    end: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "UsageWindow | None":
        if not isinstance(raw, dict):
            return None
        # ``from`` is a Python keyword, hence the rename.
        start = raw.get("from")
        end = raw.get("to")
        if start is None and end is None:
            return None
        return cls(_as_text(start), _as_text(end))

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.start:
            out["from"] = self.start
        if self.end:
            out["to"] = self.end
        return out


@dataclass
class SourceRef:
    """One ``sources[]`` entry. ``resource`` is the only required subfield."""

    resource: str
    id: str | None = None
    title: str | None = None
    author: str | None = None
    usage_count: int | None = None
    last_modified: str | None = None
    usage_window: UsageWindow | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "SourceRef | None":
        if isinstance(raw, str):
            text = raw.strip()
            return cls(resource=text) if text else None
        if not isinstance(raw, dict):
            return None
        resource = _as_text(raw.get("resource"))
        if not resource:
            return None
        count = raw.get("usage_count")
        try:
            usage_count = int(count) if count is not None else None
        except (TypeError, ValueError):
            usage_count = None
        return cls(
            resource=resource,
            id=_as_text(raw.get("id")),
            title=_as_text(raw.get("title")),
            author=_as_text(raw.get("author")),
            usage_count=usage_count,
            last_modified=_as_text(raw.get("last_modified")),
            usage_window=UsageWindow.from_raw(raw.get("usage_window")),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"resource": self.resource}
        for key in ("id", "title", "author", "last_modified"):
            val = getattr(self, key)
            if val:
                out[key] = val
        if self.usage_count is not None:
            out["usage_count"] = self.usage_count
        if self.usage_window:
            out["usage_window"] = self.usage_window.as_dict()
        return out


@dataclass
class Attribution:
    """A ``{by, at}`` pair — used by ``generated`` and each ``verified`` entry."""

    by: Actor | None = None
    at: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "Attribution | None":
        if not isinstance(raw, dict):
            return None
        by = parse_actor(raw.get("by"))
        at = _as_text(raw.get("at"))
        if by is None and not at:
            return None
        return cls(by=by, at=at)

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.by:
            out["by"] = self.by.raw
        if self.at:
            out["at"] = self.at
        return out


@dataclass
class ConceptMeta:
    """Normalized concept metadata, merged across spec versions."""

    rel_path: str = ""
    detected_version: str = "0.1"

    # Core
    type: str | None = None
    okf_type: str | None = None
    title: str | None = None
    description: str | None = None
    resource: str | None = None
    tags: list[str] = field(default_factory=list)

    # Trust (v0.2)
    generated: Attribution | None = None
    verified: list[Attribution] = field(default_factory=list)

    # Provenance (v0.2)
    sources: list[SourceRef] = field(default_factory=list)
    usage_window: UsageWindow | None = None

    # Lifecycle (v0.2)
    status: str | None = None
    stale_after: str | None = None

    # Legacy (v0.1) — kept so a dual-version read can fall back
    legacy_timestamp: str | None = None
    legacy_citations: list[str] = field(default_factory=list)

    @property
    def generated_at(self) -> str | None:
        """v0.2 ``generated.at``, falling back to the v0.1 ``timestamp``.

        SPEC §13.1 supersedes ``timestamp`` with ``generated``, but only says
        consumers **MAY** fall back. Apo does, so notes written under either
        version answer "when did this last change?" the same way.
        """
        if self.generated and self.generated.at:
            return self.generated.at
        return self.legacy_timestamp

    @property
    def source_refs(self) -> list[SourceRef]:
        """``sources`` if present, else the legacy ``# Citations`` body list.

        SPEC §13.1: consumers **SHOULD** read ``sources`` and **MAY** still
        parse the v0.1 body list.
        """
        if self.sources:
            return self.sources
        return [SourceRef(resource=c) for c in self.legacy_citations]

    @property
    def is_human_verified(self) -> bool:
        """SPEC §7 — trust tiers key off the ``human:`` actor prefix."""
        return any(v.by is not None and v.by.is_human for v in self.verified)

    def is_stale(self, now: date | None = None) -> bool:
        """True when ``stale_after`` is in the past. Absent field is never stale."""
        if not self.stale_after:
            return False
        threshold = _as_date(self.stale_after)
        if threshold is None:
            return False
        today = now or datetime.now(timezone.utc).date()
        return today > threshold

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"okf_version": self.detected_version}
        for key in ("type", "okf_type", "title", "description", "resource", "status", "stale_after"):
            val = getattr(self, key)
            if val:
                out[key] = val
        if self.tags:
            out["tags"] = list(self.tags)
        if self.generated:
            out["generated"] = self.generated.as_dict()
        if self.verified:
            out["verified"] = [v.as_dict() for v in self.verified]
        if self.sources:
            out["sources"] = [s.as_dict() for s in self.sources]
        if self.usage_window:
            out["usage_window"] = self.usage_window.as_dict()
        if self.generated_at:
            out["generated_at"] = self.generated_at
        return out


def _as_text(val: Any) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _as_date(val: str) -> date | None:
    text = val.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def as_list(raw: Any) -> list[Any]:
    """Normalize scalar / mapping / list into a list.

    SPEC §11 requires consumers to treat a bare ``verified`` mapping as a
    one-element list.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]
