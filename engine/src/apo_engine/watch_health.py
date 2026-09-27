"""Watch-loop hook health — per-vault heartbeat for the watcher's peripheral hooks.

`_watch_one` (``watch.py``) runs three pieces of business logic every cycle
alongside its actual job (sole ``index.db`` writer): git-sync, Optima-vault
merge, and desk/contracts reprojection. Each is wrapped in ``try/except
Exception`` today, which only guards against a call that *raises* — nothing
previously recorded whether a hook was ticking at all, so a hang (blocks
without raising) was invisible. See ``docs/watcher-scheduler-separation.md``
for the full problem framing; the reviewed scope is exactly this module: make
a stall *visible*, not build a thread-per-hook scheduler.

The watcher process and the process asking "is it healthy" (CLI ``doctor``,
MCP ``index_health``/``memory_status``) are usually different processes, so
this is a small persisted heartbeat file per vault (the same shape as
``git_sync``'s own ``.apo/git-sync-status.json``), not in-memory state.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Known hook names — informational only, not enforced (callers may record
# any string key).
GIT_SYNC = "git_sync"
OPTIMA_MERGE = "optima_merge"
DESK_REPROJECT = "desk_reproject"

_STATUS_REL = Path(".apo") / "watch-hooks.json"

# Throttle disk writes across a bursty event-driven loop (cycles can run at
# sub-second cadence) — an "ok" tick only needs to land within a few seconds
# to be useful for stall detection. A failing tick always writes immediately.
_WRITE_MIN_GAP_S = 5.0

# A hook that hasn't ticked in this long is flagged stale regardless of the
# watcher's configured poll/debounce interval — simple absolute bound rather
# than one derived per-vault from config, on the theory that "hasn't run in
# 5 minutes" is worth a look no matter how the watcher is tuned.
STALE_AFTER_S = 300.0


class HookHealthTracker:
    """Records last-tick timestamp/duration/outcome per hook, one per vault.

    Cheap enough to call every watch cycle: state lives in memory and the
    on-disk write is throttled (except on failure, which always writes
    immediately — a stall or error should be visible without waiting out
    the throttle window).
    """

    def __init__(self, vault_root: Path) -> None:
        self.path = vault_root / _STATUS_REL
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._last_write = 0.0

    def record(
        self,
        hook: str,
        *,
        seconds: float,
        ok: bool,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._state[hook] = {
                "last_tick_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_tick_seconds": round(max(0.0, seconds), 3),
                "ok": bool(ok),
                "error": (error or None),
            }
            snapshot = dict(self._state)
        self._maybe_write(snapshot, force=not ok)

    def _maybe_write(self, snapshot: dict[str, Any], *, force: bool) -> None:
        now = time.monotonic()
        if not force and (now - self._last_write) < _WRITE_MIN_GAP_S:
            return
        self._last_write = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass


def read_hook_health(vault_root: Path) -> dict[str, Any]:
    """Read a vault's persisted hook-health snapshot; ``{}`` if none yet."""
    path = vault_root / _STATUS_REL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_health_flags(hooks: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    """``index_health()``-style flags for a hook snapshot: errored or stale ticks."""
    now = now or datetime.now(timezone.utc)
    flags: list[str] = []
    for name, row in sorted(hooks.items()):
        if not isinstance(row, dict):
            continue
        if row.get("ok") is False:
            flags.append(f"hook_error:{name}")
            continue
        at_raw = row.get("last_tick_at")
        if not at_raw:
            continue
        try:
            at = datetime.fromisoformat(str(at_raw))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if (now - at).total_seconds() > STALE_AFTER_S:
            flags.append(f"hook_stale:{name}")
    return flags
