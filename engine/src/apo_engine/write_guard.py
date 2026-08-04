"""Optimistic write preconditions — file mtime plus FM/body/section hashes.

``expected_mtime`` remains the fast whole-file check. When the file mtime has
advanced from an unrelated edit, a scoped write may still proceed if the
bytes it touches are unchanged:

- frontmatter-only ops → ``expected_frontmatter_hash`` (or process-local cache)
- body / section / chunk ops → ``expected_body_hash`` / ``expected_content_hash``

Hashes use the same 16-hex blake2b as index ``chunks.content_hash``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from apo_engine.core import _content_hash
from apo_engine.markdown_patch import (
    Section,
    _frontmatter_bounds,
    _normalize_heading,
    _scope_heading_from_op,
    _target_heading_from_op,
    find_section,
    normalize_lines,
    section_from_chunk,
)

content_hash = _content_hash

RegionKind = Literal["frontmatter", "body", "section", "file"]


@dataclass(frozen=True)
class WriteRegions:
    """Which byte ranges a mutate will touch."""

    frontmatter: bool = False
    body: bool = False  # whole markdown body (after FM), not a single section
    sections: tuple[str, ...] = ()  # heading strings as provided by the caller
    whole_file: bool = False  # overwrite / unscoped replace / YAML

    @property
    def scoped(self) -> bool:
        if self.whole_file:
            return False
        return self.frontmatter or self.body or bool(self.sections)


@dataclass
class PathTouch:
    """Process-local snapshot from a recent read/expand/write."""

    mono: float
    mtime: float | None = None
    frontmatter_hash: str | None = None
    body_hash: str | None = None
    sections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RegionHashes:
    frontmatter_hash: str | None
    body_hash: str | None

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.frontmatter_hash is not None:
            out["frontmatter_hash"] = self.frontmatter_hash
        if self.body_hash is not None:
            out["body_hash"] = self.body_hash
        return out


def file_region_hashes(content: str) -> RegionHashes:
    lines = normalize_lines(content)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return RegionHashes(
            frontmatter_hash=None,
            body_hash=content_hash("\n".join(lines)),
        )
    _start, end = bounds
    fm = "\n".join(lines[: end + 1])
    body = "\n".join(lines[end + 1 :])
    return RegionHashes(
        frontmatter_hash=content_hash(fm),
        body_hash=content_hash(body),
    )


def section_content_hash(lines: list[str], section: Section) -> str:
    start = section.heading_line if section.title else section.body_start
    return content_hash("\n".join(lines[start : section.body_end]))


def section_hash_for_heading(content: str, heading: str) -> str | None:
    lines = normalize_lines(content)
    try:
        section = find_section(lines, heading)
    except Exception:
        return None
    return section_content_hash(lines, section)


def classify_append_regions(
    *,
    heading: str | None,
    from_chunk: bool,
) -> WriteRegions:
    if heading or from_chunk:
        return WriteRegions(sections=(heading,) if heading else ("__chunk__",))
    return WriteRegions(body=True)


def classify_patch_regions(ops: list[dict[str, Any]], *, yaml_note: bool = False) -> WriteRegions:
    if yaml_note:
        return WriteRegions(whole_file=True)

    fm = False
    body = False
    sections: list[str] = []
    seen: set[str] = set()

    for op in ops:
        kind = op.get("op")
        if kind in ("set_field", "delete_field"):
            fm = True
            continue
        if kind == "append_eof":
            body = True
            continue
        if kind in ("append", "prepend", "replace_section"):
            heading = _target_heading_from_op(op)
            if heading:
                key = _normalize_heading(heading)
                if key not in seen:
                    seen.add(key)
                    sections.append(heading)
            else:
                body = True
            continue
        if kind == "replace_text":
            heading = _scope_heading_from_op(op)
            if heading:
                key = _normalize_heading(heading)
                if key not in seen:
                    seen.add(key)
                    sections.append(heading)
            else:
                body = True
            continue
        # Unknown op → be conservative
        return WriteRegions(whole_file=True)

    if not fm and not body and not sections:
        return WriteRegions(whole_file=True)
    return WriteRegions(frontmatter=fm, body=body, sections=tuple(sections))


def _touch_section_hash(touch: PathTouch | None, heading: str) -> str | None:
    if touch is None:
        return None
    return touch.sections.get(_normalize_heading(heading))


def _resolve_expected(
    *,
    explicit: str | None,
    from_touch: str | None,
) -> str | None:
    return (explicit or "").strip() or from_touch or None


def check_write_precondition(
    *,
    path: str,
    actual_mtime: float | None,
    expected_mtime: float | None,
    content: str | None,
    regions: WriteRegions,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    touch: PathTouch | None = None,
    chunk_section: Section | None = None,
) -> dict[str, Any] | None:
    """Return an error dict on conflict, else ``None`` (allow write).

    Fast path: ``expected_mtime`` matches → allow without reading region bytes.
    Soft path: mtime advanced (or omitted) but every touched region still matches
    an explicit hash or a process-local touch snapshot for that ``expected_mtime``.
    """
    has_mtime = expected_mtime is not None
    has_region = any(
        x is not None and str(x).strip()
        for x in (
            expected_frontmatter_hash,
            expected_body_hash,
            expected_content_hash,
        )
    )
    if not has_mtime and not has_region:
        return None

    if has_mtime and actual_mtime is not None:
        if abs(float(actual_mtime) - float(expected_mtime)) <= 1e-6:
            return None

    # mtime missing/mismatch — try region hashes (explicit or same-mtime cache).
    cached: PathTouch | None = None
    if (
        touch is not None
        and has_mtime
        and touch.mtime is not None
        and abs(float(touch.mtime) - float(expected_mtime)) <= 1e-6
    ):
        cached = touch

    if content is None:
        return _stale(path, expected_mtime, actual_mtime, reason="whole_file")

    lines = normalize_lines(content)
    current = file_region_hashes(content)

    # Whole-file overwrite: both halves (or body-only when no FM) must match.
    if regions.whole_file:
        want_fm = _resolve_expected(
            explicit=expected_frontmatter_hash,
            from_touch=cached.frontmatter_hash if cached else None,
        )
        want_body = _resolve_expected(
            explicit=expected_body_hash or expected_content_hash,
            from_touch=cached.body_hash if cached else None,
        )
        if current.frontmatter_hash is None:
            if want_body is None or want_body != current.body_hash:
                return _stale(
                    path,
                    expected_mtime,
                    actual_mtime,
                    reason="body",
                    expected_hash=want_body,
                    actual_hash=current.body_hash,
                )
            return None
        if want_fm is None or want_body is None:
            return _stale(path, expected_mtime, actual_mtime, reason="whole_file")
        if want_fm != current.frontmatter_hash:
            return _stale(
                path,
                expected_mtime,
                actual_mtime,
                reason="frontmatter",
                expected_hash=want_fm,
                actual_hash=current.frontmatter_hash,
            )
        if want_body != current.body_hash:
            return _stale(
                path,
                expected_mtime,
                actual_mtime,
                reason="body",
                expected_hash=want_body,
                actual_hash=current.body_hash,
            )
        return None

    if regions.frontmatter:
        want = _resolve_expected(
            explicit=expected_frontmatter_hash,
            from_touch=cached.frontmatter_hash if cached else None,
        )
        if want is None:
            return _stale(path, expected_mtime, actual_mtime, reason="frontmatter")
        if want != current.frontmatter_hash:
            return _stale(
                path,
                expected_mtime,
                actual_mtime,
                reason="frontmatter",
                expected_hash=want,
                actual_hash=current.frontmatter_hash,
            )

    if regions.body:
        want = _resolve_expected(
            explicit=expected_body_hash or expected_content_hash,
            from_touch=cached.body_hash if cached else None,
        )
        if want is None:
            return _stale(path, expected_mtime, actual_mtime, reason="body")
        if want != current.body_hash:
            return _stale(
                path,
                expected_mtime,
                actual_mtime,
                reason="body",
                expected_hash=want,
                actual_hash=current.body_hash,
            )

    for heading in regions.sections:
        if heading == "__chunk__" and chunk_section is not None:
            actual_sec = section_content_hash(lines, chunk_section)
            want = _resolve_expected(
                explicit=expected_content_hash or expected_body_hash,
                from_touch=(
                    _touch_section_hash(cached, chunk_section.title)
                    if chunk_section.title
                    else None
                ),
            )
            if want is None and _body_unchanged(
                expected_body_hash, expected_content_hash, cached, current
            ):
                continue
            if want is None:
                return _stale(path, expected_mtime, actual_mtime, reason="section")
            if want != actual_sec:
                return _stale(
                    path,
                    expected_mtime,
                    actual_mtime,
                    reason="section",
                    expected_hash=want,
                    actual_hash=actual_sec,
                )
            continue

        try:
            section = find_section(lines, heading)
        except Exception:
            return _stale(path, expected_mtime, actual_mtime, reason="section")
        actual_sec = section_content_hash(lines, section)
        want = None
        if len(regions.sections) == 1:
            want = _resolve_expected(
                explicit=expected_content_hash or expected_body_hash,
                from_touch=_touch_section_hash(cached, heading),
            )
        else:
            want = _touch_section_hash(cached, heading)
        if want is None and _body_unchanged(
            expected_body_hash, expected_content_hash, cached, current
        ):
            continue
        if want is None:
            return _stale(path, expected_mtime, actual_mtime, reason="section")
        if want != actual_sec:
            return _stale(
                path,
                expected_mtime,
                actual_mtime,
                reason="section",
                expected_hash=want,
                actual_hash=actual_sec,
            )

    return None


def _body_unchanged(
    expected_body_hash: str | None,
    expected_content_hash: str | None,
    cached: PathTouch | None,
    current: RegionHashes,
) -> bool:
    """True when the whole markdown body still matches a known precondition."""
    want = _resolve_expected(
        explicit=expected_body_hash,
        from_touch=cached.body_hash if cached else None,
    )
    # expected_content_hash is section-oriented; only treat as body when no section match path
    if want is None:
        return False
    return want == current.body_hash


def _stale(
    path: str,
    expected_mtime: float | None,
    actual_mtime: float | None,
    *,
    reason: str,
    expected_hash: str | None = None,
    actual_hash: str | None = None,
) -> dict[str, Any]:
    msg = {
        "frontmatter": (
            "frontmatter changed since the precondition; re-read before writing"
        ),
        "body": "body changed since the precondition; re-read before writing",
        "section": "section/chunk changed since the precondition; re-read before writing",
        "whole_file": "file modified since expected_mtime; re-read before writing",
    }.get(reason, "file modified since expected_mtime; re-read before writing")
    out: dict[str, Any] = {
        "ok": False,
        "path": path,
        "error": "stale_write",
        "message": msg,
        "stale_region": reason,
    }
    if expected_mtime is not None:
        out["expected_mtime"] = float(expected_mtime)
    if actual_mtime is not None:
        out["actual_mtime"] = float(actual_mtime)
    if expected_hash is not None:
        out["expected_hash"] = expected_hash
    if actual_hash is not None:
        out["actual_hash"] = actual_hash
    return out


def attach_region_hashes(
    out: dict[str, Any],
    content: str,
    *,
    heading: str | None = None,
    section: Section | None = None,
    chunk_start: int | None = None,
    chunk_level: int | None = None,
) -> dict[str, Any]:
    """Add ``frontmatter_hash`` / ``body_hash`` / ``content_hash`` to a success payload."""
    regions = file_region_hashes(content)
    out.update(regions.as_dict())
    lines = normalize_lines(content)
    sec: Section | None = section
    if sec is None and chunk_start is not None:
        sec = section_from_chunk(lines, chunk_start, int(chunk_level or 0))
    if sec is None and heading:
        try:
            sec = find_section(lines, heading)
        except Exception:
            sec = None
    if sec is not None:
        out["content_hash"] = section_content_hash(lines, sec)
    elif heading is None and chunk_start is None:
        # Unscoped read: content_hash ≡ body (or whole file when no FM)
        if regions.body_hash is not None:
            out["content_hash"] = regions.body_hash
    return out


def snapshot_from_content(
    mono: float,
    mtime: float | None,
    content: str,
    *,
    heading: str | None = None,
    section: Section | None = None,
) -> PathTouch:
    regions = file_region_hashes(content)
    touch = PathTouch(
        mono=mono,
        mtime=mtime,
        frontmatter_hash=regions.frontmatter_hash,
        body_hash=regions.body_hash,
    )
    lines = normalize_lines(content)
    sec = section
    if sec is None and heading:
        try:
            sec = find_section(lines, heading)
        except Exception:
            sec = None
    if sec is not None and sec.title:
        touch.sections[_normalize_heading(sec.title)] = section_content_hash(lines, sec)
    return touch
