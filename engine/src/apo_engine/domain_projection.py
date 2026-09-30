"""Optima domain projection exchange v1 — parse, validate, resolve.

A domain (``atlas`` or ``work``) publishes a sanitized rolling schedule as YAML
(schema ``optima.domain-projection/v1``). This module validates that payload and
answers "what is projected now?" Pure functions; no file or network access.

Semantics (from the vault's ``system/config/domain-projections.md``):

* Bounds are half-open ``[start, end)``.
* A gap inside coverage means *no projected block*, never "free".
* Outside coverage is *unknown*.
* A future ``verified_at``, invalid snapshot, or missing domain is an error or
  missing input, never "free".
* ``stale`` is informational: a stale snapshot is still a usable forecast.

``summarize`` returns only values that stay stable between publishes (no ages),
so writing it into ``current.yaml`` does not defeat the merge's unchanged-payload
skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "optima.domain-projection/v1"
DOMAINS = ("atlas", "work")
# Merge precedence, highest first (free precedes habit_projection).
KIND_PRECEDENCE = (
    "incident",
    "meeting",
    "family_duty",
    "personal",
    "focus",
    "work_private",
    "default_work",
    "free",
    "habit_projection",
)
CONFIDENCES = ("low", "medium", "high")
TOP_KEYS = frozenset(
    {"title", "status", "schema", "domain", "timezone", "verified_at", "coverage", "blocks"}
)
BLOCK_KEYS = frozenset({"id", "kind", "start", "end", "confidence"})
_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_CLOCK_SKEW = timedelta(minutes=1)


class ProjectionError(ValueError):
    """The payload is not a valid v1 projection. Never treat the domain as free."""


@dataclass(frozen=True)
class Block:
    id: str
    kind: str
    start: datetime
    end: datetime
    confidence: str


@dataclass(frozen=True)
class Projection:
    domain: str
    timezone: str
    verified_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Resolution:
    state: str  # active | none | outside_coverage
    active: Block | None = None
    next_block: Block | None = None


def _aware(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProjectionError(f"{what} is not ISO 8601: {value!r}") from exc
    else:
        raise ProjectionError(f"{what} must be an ISO 8601 timestamp")
    if dt.tzinfo is None:
        raise ProjectionError(f"{what} has no UTC offset: {value!r}")
    return dt


def parse(data: Any, *, expect_domain: str, now: datetime) -> Projection:
    """Validate ``data`` strictly and return a ``Projection``. Raises ``ProjectionError``."""
    if not isinstance(data, dict):
        raise ProjectionError("projection must be a mapping")
    keys = set(data)
    if keys != TOP_KEYS:
        missing, extra = sorted(TOP_KEYS - keys), sorted(keys - TOP_KEYS)
        raise ProjectionError(f"keys mismatch (missing={missing}, unknown={extra})")
    if data["schema"] != SCHEMA:
        raise ProjectionError(f"schema must be {SCHEMA}, got {data['schema']!r}")
    if data["domain"] not in DOMAINS:
        raise ProjectionError(f"domain must be one of {DOMAINS}, got {data['domain']!r}")
    if data["domain"] != expect_domain:
        raise ProjectionError(f"domain {data['domain']!r} published at the {expect_domain!r} path")
    try:
        ZoneInfo(str(data["timezone"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProjectionError(f"timezone is not an IANA zone: {data['timezone']!r}") from exc

    verified_at = _aware(data["verified_at"], "verified_at")
    if verified_at > now + _CLOCK_SKEW:
        raise ProjectionError(f"verified_at is in the future: {data['verified_at']!r}")

    cov = data["coverage"]
    if not isinstance(cov, dict) or set(cov) != {"start", "end", "complete"}:
        raise ProjectionError("coverage must be {start, end, complete}")
    if cov["complete"] is not True:
        raise ProjectionError("coverage.complete must be true")
    cov_start, cov_end = _aware(cov["start"], "coverage.start"), _aware(cov["end"], "coverage.end")
    if cov_end <= cov_start:
        raise ProjectionError("coverage.end must follow coverage.start")

    raw_blocks = data["blocks"]
    if not isinstance(raw_blocks, list):
        raise ProjectionError("blocks must be a list")
    blocks: list[Block] = []
    seen: set[str] = set()
    previous: tuple[datetime, str] | None = None
    for raw in raw_blocks:
        if not isinstance(raw, dict) or set(raw) != BLOCK_KEYS:
            raise ProjectionError(f"each block must have exactly {sorted(BLOCK_KEYS)}")
        bid = str(raw["id"])
        if not _ID_RE.match(bid) or bid in seen:
            raise ProjectionError(f"bad or duplicate block id {bid!r}")
        seen.add(bid)
        if raw["kind"] not in KIND_PRECEDENCE:
            raise ProjectionError(f"unknown block kind {raw['kind']!r}")
        if raw["confidence"] not in CONFIDENCES:
            raise ProjectionError(f"unknown block confidence {raw['confidence']!r}")
        start, end = _aware(raw["start"], "block.start"), _aware(raw["end"], "block.end")
        if not (cov_start <= start < end <= cov_end):
            raise ProjectionError(f"block {bid} is empty or outside coverage")
        if previous is not None and (start, bid) < previous:
            raise ProjectionError("blocks must be sorted by start then id")
        previous = (start, bid)
        blocks.append(Block(bid, raw["kind"], start, end, raw["confidence"]))

    return Projection(
        domain=data["domain"],
        timezone=str(data["timezone"]),
        verified_at=verified_at,
        coverage_start=cov_start,
        coverage_end=cov_end,
        blocks=tuple(blocks),
    )


def _rank(block: Block) -> tuple[int, datetime, str]:
    return (KIND_PRECEDENCE.index(block.kind), block.start, block.id)


def resolve(projection: Projection, now: datetime) -> Resolution:
    """What is projected at ``now``: the highest-precedence block covering it."""
    if not (projection.coverage_start <= now < projection.coverage_end):
        return Resolution("outside_coverage")
    covering = [b for b in projection.blocks if b.start <= now < b.end]
    upcoming = [b for b in projection.blocks if b.start > now]
    next_block = min(upcoming, key=lambda b: (b.start, b.id), default=None)
    if not covering:
        return Resolution("none", None, next_block)
    return Resolution("active", min(covering, key=_rank), next_block)


def _block_summary(block: Block | None, *, with_end: bool) -> dict[str, Any] | None:
    if block is None:
        return None
    out: dict[str, Any] = {"kind": block.kind, "start": block.start.isoformat()}
    if with_end:
        out["end"] = block.end.isoformat()
        out["confidence"] = block.confidence
    return out


def summarize(
    projection: Projection, now: datetime, *, stale_after_minutes: int
) -> dict[str, Any]:
    """Stable summary for ``current.yaml`` (no ages; ``stale`` flips once)."""
    res = resolve(projection, now)
    stale = now - projection.verified_at > timedelta(minutes=stale_after_minutes)
    return {
        "state": "ok",
        "verified_at": projection.verified_at.isoformat(),
        "coverage_end": projection.coverage_end.isoformat(),
        "stale": stale,
        "position": res.state,
        "active": _block_summary(res.active, with_end=True),
        "next": _block_summary(res.next_block, with_end=False),
    }
