"""Write guards for ops mutators: mtime compare-and-swap checks, process-local
touch tracking, index enqueue, and the shared write-finalization step."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from apo_engine import archival_contract, deferred as index_deferred, note_lint, vaults
from apo_engine.write_guard import (
    check_write_precondition,
    PathTouch,
    snapshot_from_content,
    WriteRegions,
)
from apo_engine.ops_common import (
    _attach_flaws,
    _attach_tip,
    _binding,
    _err,
    _stamp_qualified,
    OpsError,
)
from apo_engine.ops_admin import _attach_watcher_tip


def _check_mtime(full: Path, expected: float | None, path: str) -> dict[str, Any] | None:
    """Whole-file mtime CAS (place_note / simple paths). Prefer ``_guard_write``."""
    if expected is None or not full.exists():
        return None
    actual = full.stat().st_mtime
    if abs(actual - float(expected)) > 1e-6:
        return _err(
            path=path,
            error="stale_write",
            message="file modified since expected_mtime; re-read before writing",
            expected_mtime=float(expected),
            actual_mtime=actual,
            stale_region="whole_file",
        )
    return None


def _guard_write(
    full: Path,
    path: str,
    *,
    vault: str,
    regions: WriteRegions,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    content: str | None = None,
    chunk_section: Any = None,
) -> dict[str, Any] | None:
    """mtime + FM/body/section precondition. ``None`` means allow."""
    if (
        expected_mtime is None
        and not (expected_frontmatter_hash or "").strip()
        and not (expected_body_hash or "").strip()
        and not (expected_content_hash or "").strip()
    ):
        return None
    if not full.exists():
        return None
    actual = full.stat().st_mtime
    with _touches_lock:
        touch = _recent_touches.get(_write_key(vault, path))
    err = check_write_precondition(
        path=path,
        actual_mtime=actual,
        expected_mtime=expected_mtime,
        content=content,
        regions=regions,
        expected_frontmatter_hash=expected_frontmatter_hash,
        expected_body_hash=expected_body_hash,
        expected_content_hash=expected_content_hash,
        touch=touch if isinstance(touch, PathTouch) else None,
        chunk_section=chunk_section,
    )
    return err


def _enqueue_index(b: vaults.VaultBinding, full: Path) -> None:
    try:
        index_deferred.enqueue_index(b.collection, str(full.resolve()))
    except Exception:
        pass


def _enqueue_purge(b: vaults.VaultBinding, full: Path) -> bool:
    try:
        index_deferred.enqueue_purge(b.collection, str(full.resolve()))
        return True
    except Exception:
        return False


_MTIME_TIP = (
    "pass expected_mtime from the prior read/write for this path to avoid stale_write"
)

# Process-local: vault:path → PathTouch (mtime + region hashes).
# Touches from read_note / expand_chunk / successful writes drive soft mtime tips
# and FM/body/section precondition fallbacks when expected_mtime is stale.
_recent_touches: dict[str, PathTouch] = {}
# Guards every read-modify-write of ``_recent_touches``. The per-note flock does not
# cover it: reads (read_note) touch the map without taking a note lock, and prune
# iterates the whole map. RLock because ``_record_path_touch`` calls the prune.
_touches_lock = threading.RLock()
_RECENT_TOUCH_TTL_S = 300.0
# Back-compat alias for tests that clear the habit map.
_recent_writes = _recent_touches


def _write_key(vault: str, path: str) -> str:
    return f"{(vault or '').strip()}:{path.replace(chr(92), '/')}"


def _reject_if_ref(ref: str, *, tool: str = "write") -> dict[str, Any] | None:
    """Hard-reject ``ref=`` on mutators (git catalog is read-only)."""
    r = (ref or "").strip()
    if not r:
        return None
    return _err(
        error="bad_request",
        message=(
            f"{tool} does not support ref= — git catalog views are read-only; "
            "mutate the working tree (or a jj workspace) then filter/read with ref= after export"
        ),
    )


def _prune_recent_touches(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _touches_lock:
        stale = [
            k
            for k, touch in _recent_touches.items()
            if now - touch.mono > _RECENT_TOUCH_TTL_S
        ]
        for k in stale:
            _recent_touches.pop(k, None)


def _record_path_touch(
    vault: str,
    path: str,
    file_mtime: float | None = None,
    content: str | None = None,
    *,
    heading: str | None = None,
    frontmatter_hash: str | None = None,
    body_hash: str | None = None,
    section_hash: str | None = None,
) -> None:
    """Record a successful read/expand/write so a follow-up write can tip expected_mtime."""
    now = time.monotonic()
    with _touches_lock:
        _prune_recent_touches(now)
        key = _write_key(vault, path)
        if frontmatter_hash is not None or body_hash is not None:
            touch = PathTouch(
                mono=now,
                mtime=file_mtime,
                frontmatter_hash=frontmatter_hash,
                body_hash=body_hash,
            )
            if heading and section_hash:
                from apo_engine.markdown_patch import _normalize_heading

                touch.sections[_normalize_heading(heading)] = section_hash
            _recent_touches[key] = touch
            return
        if content is not None:
            _recent_touches[key] = snapshot_from_content(
                now, file_mtime, content, heading=heading
            )
            return
        prior = _recent_touches.get(key)
        if prior is not None:
            prior.mono = now
            if file_mtime is not None:
                prior.mtime = file_mtime
            return
        _recent_touches[key] = PathTouch(mono=now, mtime=file_mtime)


def _attach_mtime_tip(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
    content: str | None = None,
) -> dict[str, Any]:
    """Tip when a follow-up write omits ``expected_mtime`` after a recent read/write."""
    with _touches_lock:
        now = time.monotonic()
        _prune_recent_touches(now)

        key = _write_key(vault or str(out.get("vault") or ""), path)
        prior = _recent_touches.get(key)
        if (
            expected_mtime is None
            and prior is not None
            and (now - prior.mono) <= _RECENT_TOUCH_TTL_S
        ):
            stored_mtime = prior.mtime
            if stored_mtime is not None:
                tip = f"pass expected_mtime={stored_mtime} to avoid stale_write"
            else:
                tip = _MTIME_TIP
            out = _attach_tip(out, tip)
        if out.get("ok"):
            new_mtime = out.get("mtime")
            file_mtime = float(new_mtime) if isinstance(new_mtime, (int, float)) else (
                prior.mtime if prior else None
            )
            fm = out.get("frontmatter_hash")
            body = out.get("body_hash")
            if isinstance(fm, str) or isinstance(body, str):
                _recent_touches[key] = PathTouch(
                    mono=now,
                    mtime=file_mtime,
                    frontmatter_hash=fm if isinstance(fm, str) else None,
                    body_hash=body if isinstance(body, str) else None,
                )
            elif content is not None:
                _recent_touches[key] = snapshot_from_content(now, file_mtime, content)
            elif prior is not None:
                prior.mono = now
                prior.mtime = file_mtime
            else:
                _recent_touches[key] = PathTouch(mono=now, mtime=file_mtime)
    return out


def _prepare_write_content(
    content: str,
    *,
    path: str,
    vault: str,
    okf_result: Any | None = None,
    format_only: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Auto-fix trailing WS before disk write; collect OKF + format flaws.

    ``format_only`` skips OKF flaw mapping (append_note path).
    """
    flaws: list[dict[str, Any]] = []
    if okf_result is not None and not format_only:
        flaws.extend(
            note_lint.flaws_from_okf(okf_result, path=path, vault=vault)
        )
    text, fmt_flaws = note_lint.apply_auto_fixes(
        content, path=path, vault=vault, enabled=True
    )
    flaws.extend(fmt_flaws)
    return text, flaws


def _attach_archival_write_flaws(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    content: str | None = None,
) -> dict[str, Any]:
    """Post-write archival suggest check (eligible + blocked_todos only)."""
    if not out.get("ok") or not path:
        return out
    try:
        b = _binding(vault or str(out.get("vault") or ""))
        root = b.resolved().root
    except OpsError:
        return out
    flaws, tip = archival_contract.evaluate_write_path(
        root, path, content=content
    )
    if tip:
        out = _attach_tip(out, tip)
    return _attach_flaws(out, flaws)


def _finalize_write(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
    content: str | None = None,
) -> dict[str, Any]:
    out = _attach_mtime_tip(
        out,
        vault=vault,
        path=path,
        expected_mtime=expected_mtime,
        content=content,
    )
    out = _attach_archival_write_flaws(
        out, vault=vault, path=path, content=content
    )
    out = _stamp_qualified(out, vault=vault, path=path)
    return _attach_watcher_tip(out)
