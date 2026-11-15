"""Ops admin surface: watcher/index status, reindex, stats, index health, and
git sync/refs."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from apo_engine import (
    config,
    core,
    deferred as index_deferred,
    git_catalog,
    git_sync,
    vaults,
)
from apo_engine.ops_common import _binding, _err, _sqlite_index_err, OpsError


WATCH_PID_FILE = Path.home() / ".apo" / "watch.pid"

_WATCHER_TIP = (
    "watcher not running — write is on disk and enqueued, but search won't "
    "update until apo-engine watch is up (just watch-status)"
)


def watcher_status() -> dict[str, Any]:
    """Best-effort liveness check for the watcher PID file.

    PID existence alone (os.kill(pid, 0)) isn't process *identity* — if the watcher died
    and the PID was later recycled by an unrelated process, that check false-positives.
    Cross-check /proc/<pid>/cmdline where available (Linux); degrade to existence-only
    elsewhere rather than fail the check outright.
    """
    status: dict[str, Any] = {"pid_file": str(WATCH_PID_FILE), "running": False}
    try:
        pid = int(WATCH_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return status
    try:
        os.kill(pid, 0)
    except OSError:
        return status
    status["pid"] = pid
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            cmdline = cmdline_path.read_bytes().decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if cmdline and not ("apo-engine" in cmdline and "watch" in cmdline):
            status["warning"] = (
                f"pid {pid} is alive but doesn't look like apo-engine watch "
                "(stale/recycled pid?)"
            )
            return status
    status["running"] = True
    return status


def index_visibility(*, woken: bool = True, running: bool | None = None) -> dict[str, Any]:
    """Documented bound on how long a completed write stays invisible to search.

    MCP never writes ``index.db``; the watcher is the sole writer. That means a
    successful write is durable on disk immediately but is not searchable until
    the watcher indexes it, and until now no bound on that delay was written
    down anywhere.

    The bound is **scheduling latency only**:

    * watcher running, write enqueued with ``wake`` (the default on every write
      op) — ``APO_WATCH_DEBOUNCE`` seconds, since the wake file makes the
      watcher drain queues immediately and the per-path debounce timer is the
      only thing left in the way.
    * watcher running, wake missed / fs-event-only — add ``WATCH_INTERVAL``,
      the periodic hash scan.
    * watcher not running — unbounded. Nothing consumes the queue.

    Embedding time is **not** included: ``embed()`` is a network call to Ollama
    and its duration depends on batch size and model load. Callers that need
    read-after-write certainty must poll for the content rather than sleep for
    ``bound_seconds``.
    """
    alive = watcher_status().get("running") if running is None else running
    debounce = float(config.WATCH_DEBOUNCE)
    poll = float(config.WATCH_POLL_INTERVAL)

    if not alive:
        return {
            "watcher_running": False,
            "bound_seconds": None,
            "path": "blocked",
            "note": _WATCHER_TIP,
        }
    bound = debounce if woken else debounce + poll
    return {
        "watcher_running": True,
        "bound_seconds": round(bound, 1),
        "path": "wake" if woken else "poll",
        "debounce_seconds": debounce,
        "poll_interval_seconds": poll,
        "note": "scheduling bound only; embed() time is additional",
    }


def reindex(
    vault: str = "",
    *,
    mode: str = "flush",
    force: bool = False,
    wait: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Watcher-aware index maintenance — shared by ``apo-engine index`` and the
    MCP ``apo_admin`` ``reindex`` capability, so both surfaces mean the same thing.

    ``mode="flush"``: wake the watcher to drain its already-enqueued deferred
    index queue (``vault=""`` targets every registered vault). Does not itself
    index anything — a no-op if no watcher is running.

    ``mode="rebuild"``: full vault scan (``force=True`` drops and rebuilds from
    scratch, matching ``core.index_vault(rebuild=...)``; single vault only).
    A live watcher is the sole ``index.db`` writer (docs/index-concurrency.md),
    so when one is running this only *signals* it — the same
    ``deferred.signal_rebuild`` + wake mechanism the legacy admin handlers used —
    rather than running a second concurrent writer in this process. With no
    watcher live there is no concurrent-writer risk, so this runs the scan
    inline (today's direct ``core.index_vault`` behavior).

    ``wait=True`` (rebuild + live watcher only) polls up to ``timeout`` seconds
    for the signaled rebuild to be picked up and finish, using the rebuild
    request marker and a busy marker the watcher holds for the duration of its
    ``index_vault`` call (see ``deferred.rebuild_pending`` / ``rebuild_running``).
    This is a narrow completion signal for this one wait loop, not a general
    index-health report.
    """
    mode_s = (mode or "flush").strip().lower()
    if mode_s not in ("flush", "rebuild"):
        return _err(error="bad_request", message="mode must be flush or rebuild")

    if mode_s == "flush":
        vault_s = (vault or "").strip()
        if vault_s:
            try:
                targets = [_binding(vault_s)]
            except OpsError as e:
                return _err(error=e.code, message=e.message)
        else:
            _default, bindings = vaults.load_bindings()
            targets = list(bindings.values())
        queued = 0
        for b in targets:
            index_deferred.touch_wake(b.collection)
            queued += len(index_deferred.load_index_queue(b.collection))
        watcher = watcher_status()
        out: dict[str, Any] = {
            "ok": True,
            "mode": "flush",
            "queued": queued,
            "signaled": True,
            "watcher_running": watcher["running"],
        }
        if not watcher["running"]:
            out["warning"] = (
                "no watcher detected — the deferred queue is signaled but nothing will "
                "consume it until apo-engine watch is running (just watch-status)"
            )
        return out

    # mode == "rebuild"
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    watcher = watcher_status()
    if not watcher.get("running"):
        t0 = time.monotonic()
        try:
            with vaults.bind(b):
                stats = core.index_vault(rebuild=force)
        except Exception as e:
            if err := _sqlite_index_err(e):
                return err
            return _err(error="reindex_failed", message=str(e))
        return {
            "ok": True,
            "mode": "rebuild",
            "vault": b.name,
            "inline": True,
            "watcher_running": False,
            "force": force,
            "seconds": round(time.monotonic() - t0, 2),
            "added": stats.added,
            "changed": stats.changed,
            "removed": stats.removed,
            "chunks": stats.chunks,
        }

    index_deferred.signal_rebuild(b.collection, force=force)
    out = {
        "ok": True,
        "mode": "rebuild",
        "vault": b.name,
        "inline": False,
        "rebuild_signaled": True,
        "force": force,
        "watcher_running": True,
        "waited": False,
    }
    if not wait:
        return out

    out["waited"] = True
    out["timeout"] = float(timeout)
    def _settled() -> bool:
        return not index_deferred.rebuild_pending(b.collection) and not index_deferred.rebuild_running(
            b.collection
        )

    deadline = time.monotonic() + max(0.0, float(timeout))
    poll_s = 0.25
    completed = _settled()
    while not completed and time.monotonic() < deadline:
        time.sleep(poll_s)
        completed = _settled()
    out["completed"] = completed
    if not completed:
        out["warning"] = (
            f"rebuild still in progress after {timeout}s — it keeps running in the watcher; "
            "retry with a longer timeout, or poll apo_admin(memory_status)"
        )
    return out


def _attach_watcher_tip(out: dict[str, Any]) -> dict[str, Any]:
    """Surface missing watcher on successful writes."""
    if not out.get("ok"):
        return out
    if watcher_status().get("running"):
        return out
    existing = out.get("warning")
    out["warning"] = f"{existing}; {_WATCHER_TIP}" if existing else _WATCHER_TIP
    return out


def stats(*, vault: str = "") -> dict[str, Any]:
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    with vaults.bind(b):
        data = core.stats()
    data["ok"] = True
    data["vault"] = b.name
    return data


def index_health(*, vault: str = "") -> dict[str, Any]:
    """Per-vault index diagnostics (db/WAL sizes, row-count parity, orphans,
    backlinks blowup, embed quarantine) plus global watcher state.

    Empty ``vault`` (the default) reports every registered vault, not just the
    registry default — unlike ``stats()``/``_binding()``, since this is a
    fleet-health check, not a per-call vault selector.
    """
    default, bindings = vaults.load_bindings()
    vault_s = (vault or "").strip()
    if vault_s:
        if vault_s not in bindings:
            return _err(
                error="bad_vault",
                message=f"unknown vault {vault_s!r}; available: {sorted(bindings)}",
            )
        targets = {vault_s: bindings[vault_s]}
    else:
        targets = bindings

    out_vaults: dict[str, Any] = {}
    for name, b in targets.items():
        with vaults.bind(b):
            out_vaults[name] = core.index_health()

    return {
        "ok": True,
        "default_vault": default,
        "vaults": out_vaults,
        "watcher": watcher_status(),
        "index_visibility": index_visibility(),
    }


def git_sync_op(
    action: str = "status",
    *,
    message: str = "",
    vault: str = "",
) -> dict[str, Any]:
    """Git contract sync: status | run | pull | rebase | clear_block.

    ``run`` commits all dirty paths except ``never_commit`` / gitignore, then pushes.
    Auto path uses path-aware template message; pass ``message`` to override subject
    (tool-triggered). A Paths body trailer is always attached. ``rebase`` recovers
    from a diverged remote (fetch + rebase local commits, push if configured) when
    ``pull`` (ff-only) has blocked.
    """
    try:
        b = _binding(vault, write=True)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    act = (action or "status").strip().lower()
    if act not in ("status", "run", "pull", "rebase", "clear_block"):
        return _err(
            error="bad_action",
            message="action must be status|run|pull|rebase|clear_block",
            vault=b.name,
        )
    out = git_sync.run_action(
        root,
        act,  # type: ignore[arg-type]
        message=(message or "").strip() or None,
    )
    out.setdefault("vault", b.name)
    return out


def list_refs_op(*, vault: str = "", kind: str = "heads") -> dict[str, Any]:
    """Reachable git refs at the vault registry root (for ``ref=`` discovery)."""
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    k = (kind or "heads").strip().lower()
    try:
        refs = git_catalog.list_refs(root, kind=k)
    except git_catalog.GitCatalogError as e:
        return _err(error=e.code, message=e.message, vault=b.name, root=str(root))
    return {
        "ok": True,
        "vault": b.name,
        "root": str(root),
        "kind": k,
        "refs": refs,
        "count": len(refs),
    }
