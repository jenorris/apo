"""Vault watcher — filesystem events + deferred queue consumer (sole index writer)."""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config, core, deferred, git_sync, watch_health
from .note_format import is_note_path

# Every watcher log line — and, via the "apo" logger hierarchy, `core.py`'s
# `logging.getLogger("apo.index")` warnings — goes through this handler once
# `_configure_logging` runs. Previously: bare `print(..., flush=True)` with no
# timestamp, appended forever by launchd/`watch.sh` (988MB / 174MB observed,
# unbounded). `RotatingFileHandler` bounds it; `%(asctime)s` makes a burst of
# errors answerable ("did this happen over 10 minutes or 10 days") without
# cross-referencing the index.
_LOG_ROOT_NAME = "apo"
_LOG_MAX_BYTES = 50 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

logger = logging.getLogger("apo.watch")


def log_file_path() -> Path:
    """Resolve the watcher's rotated log file.

    ``APO_WATCH_LOG_FILE`` wins outright; else ``WATCH_PID_DIR`` (the same
    env `watch.sh` uses for the pid file) + ``watch.log``; else
    ``~/.apo/watch.log``. Kept distinct from `watch.sh`'s own stdout/stderr
    redirect target (`watch-stdout.log`) — two writers rotating the same
    path would fight each other.
    """
    override = os.environ.get("APO_WATCH_LOG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    pid_dir = os.environ.get("WATCH_PID_DIR", "").strip()
    base = Path(pid_dir).expanduser() if pid_dir else Path.home() / ".apo"
    return base / "watch.log"


def _configure_logging(verbose: bool) -> logging.Logger:
    """Idempotent: attach the rotating handler once per process.

    Handler lives on the ``"apo"`` logger (not ``"apo.watch"``) so
    ``core.py``'s ``logging.getLogger("apo.index")`` warnings inherit the
    same timestamped, rotated destination via the logging hierarchy —
    no separate wiring needed in `core.py`.
    """
    root = logging.getLogger(_LOG_ROOT_NAME)
    if not root.handlers:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(path),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


class PathDebouncer:
    """Coalesce path updates: index only after `delay` seconds of silence per path."""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, float(delay))
        self._pending: dict[Path, float] = {}
        self._lock = threading.Lock()

    def touch(self, paths: Path | list[Path] | set[Path], *, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        if isinstance(paths, Path):
            items = (paths,)
        else:
            items = paths
        with self._lock:
            for p in items:
                self._pending[p] = ts

    def ready(self, *, now: float | None = None) -> list[Path]:
        ts = time.monotonic() if now is None else now
        with self._lock:
            due = [p for p, seen in self._pending.items() if ts - seen >= self.delay]
            for p in due:
                del self._pending[p]
            return sorted(due)

    def discard(self, paths: list[Path] | set[Path]) -> None:
        with self._lock:
            for p in paths:
                self._pending.pop(p, None)

    def waiting(self) -> int:
        with self._lock:
            return len(self._pending)

    def next_due_in(self, *, now: float | None = None) -> float | None:
        """Seconds until the oldest pending path becomes ready, or None if idle."""
        ts = time.monotonic() if now is None else now
        with self._lock:
            if not self._pending:
                return None
            oldest = min(self._pending.values())
            return max(0.0, self.delay - (ts - oldest))


def _note_path(root: Path, raw: str) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
        p.relative_to(root.resolve())
    except ValueError:
        return None
    if not is_note_path(p):
        return None
    # Deleted notes still need indexing (purge); keep non-files for deletions.
    if p.is_file() or not p.exists():
        return p
    return None




def _event_path_noise(raw: str, root: Path, ignore_res: list) -> bool:
    """True if this FS event should be ignored before debounce/wake.

    Cheap segment checks reject ``.obsidian`` / ``.git`` traffic; ignore globs
    match vault-relative paths when the prefix is under ``root``.
    """
    if not raw:
        return True
    norm = str(raw).replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    if base and not is_note_path(base):
        return True
    root_s = str(root).replace("\\", "/")
    if not root_s.endswith("/"):
        root_s += "/"
    rel = None
    if norm.startswith(root_s):
        rel = norm[len(root_s):]
    elif norm.startswith(str(root) + "/"):
        rel = norm[len(str(root)) + 1 :]
    if rel is None:
        # May still be under root via unresolved path — let _note_path decide.
        return False
    for part in rel.split("/"):
        if part in {".git", ".obsidian", ".trash"}:
            return True
    return bool(core._is_ignored(rel, ignore_res))

def _format_indexed_summary(
    stats: core.QueueStats, ready: list[Path], root: Path
) -> str | None:
    """One-line ``indexed:`` summary, or ``None`` when there's nothing to say.

    Small debounced batches are the common single-save case that used to make
    this log line unanswerable (``indexed: 1 file(s)`` with no path) — name
    them when there are few enough to be readable (``<= 5``); larger batches
    stay a count so a bulk reindex doesn't spam the log.
    """
    vs = stats.vault_stats
    scan_changed = bool(vs and (vs.added or vs.changed or vs.removed))
    if not (stats.indexed or stats.purged or scan_changed):
        return None
    parts: list[str] = []
    if stats.indexed:
        if ready and len(ready) <= 5:
            names = ", ".join(p.relative_to(root).as_posix() for p in ready)
            parts.append(f"{stats.indexed} file(s) [{names}]")
        else:
            parts.append(f"{stats.indexed} file(s)")
    if stats.purged:
        parts.append(f"{stats.purged} purged")
    if scan_changed:
        parts.append(f"scan +{vs.added} ~{vs.changed} -{vs.removed}")
    return ", ".join(parts)


def _index_paths(paths: set[Path] | list[Path], *, verbose: bool) -> int:
    """Index ready paths in one embed batch. Returns files updated or purged."""
    items = list(paths)
    if not items:
        return 0
    try:
        n = core.index_files(items, verbose=verbose)
        # index_files counts active updates; also count pure deletes for the log.
        purged = sum(1 for p in items if not Path(p).is_file())
        return n if n else purged
    except (OSError, ValueError) as e:
        if verbose:
            logger.warning("skip batch: %s", e)
        return 0


def run_watch(interval: float | None = None, *, use_events: bool | None = None, verbose: bool = True) -> None:
    """Watch one or more vaults; consume deferred/purge queues; index incrementally.

    Multi-vault discovery (``APO_COLLECTION_ROOT`` / ``APO_VAULT_PATHS`` /
    ``APO_VAULTS`` shim): one watcher thread per vault. The supervisor re-reads
    the registry on ``wake-registry``, registry/collection-root mtime, and
    **hot-adds** / **soft-removes** vaults without a full process restart.
    Soft-remove keeps ``index-*.db`` / deferred queues on disk.
    """
    from . import vaults

    _configure_logging(verbose)

    _default, bindings = vaults.load_bindings()
    # Single-vault legacy (no discovery env): one thread, no supervisor.
    # When discovery is active (even with one vault), run the multi-vault
    # supervisor so hot-add / soft-remove works without a restart.
    registry_mode = vaults.discovery_active()
    if len(bindings) == 1 and not registry_mode:
        b = next(iter(bindings.values()))
        with vaults.bind(b):
            _watch_one(b, interval=interval, use_events=use_events, verbose=verbose)
        return

    if verbose:
        names = ", ".join(sorted(bindings))
        logger.info("Multi-vault watch: %s", names)

    process_stop = threading.Event()
    # name -> (VaultBinding, Thread, per-vault stop Event)
    active: dict[str, tuple[object, threading.Thread, threading.Event]] = {}
    zombie_vaults: set[str] = set()

    def spawn(b: vaults.VaultBinding, *, initial_rebuild: bool = False) -> None:
        if b.name in zombie_vaults:
            if verbose:
                logger.info(
                    "[registry] skip respawn for vault %r — prior thread still alive",
                    b.name,
                )
            return
        if initial_rebuild:
            try:
                deferred.signal_rebuild(b.collection, force=False)
            except OSError:
                pass
        vault_stop = threading.Event()

        def _run(binding: vaults.VaultBinding = b, stop_ev: threading.Event = vault_stop) -> None:
            with vaults.bind(binding):
                try:
                    _watch_one(
                        binding,
                        interval=interval,
                        use_events=use_events,
                        verbose=verbose,
                        stop=stop_ev,
                    )
                except (Exception, SystemExit) as e:
                    # SystemExit escapes `except Exception` and is dropped silently by
                    # threading.excepthook — surface it so one vault cannot fail invisibly.
                    if verbose:
                        logger.error(
                            "vault %s watch fatal: %s: %s",
                            binding.name,
                            type(e).__name__,
                            e,
                        )

        t = threading.Thread(target=_run, name=f"apo-watch-{b.name}", daemon=True)
        t.start()
        active[b.name] = (b, t, vault_stop)
        if verbose and initial_rebuild:
            logger.info("[registry] hot-added vault %r → %s", b.name, b.root)

    def soft_remove(name: str, *, reason: str) -> None:
        entry = active.pop(name, None)
        if entry is None:
            return
        old_b, t, vault_stop = entry
        vault_stop.set()
        t.join(timeout=5)
        if t.is_alive():
            zombie_vaults.add(name)
            if verbose:
                logger.warning(
                    "[registry] soft-removed vault %r (%s) — "
                    "thread still alive; index/deferred kept (%s); "
                    "will not respawn until watcher restart",
                    name,
                    reason,
                    old_b.index.name,
                )
            return
        if verbose:
            logger.info(
                "[registry] soft-removed vault %r (%s) — index/deferred kept (%s)",
                name,
                reason,
                old_b.index.name,
            )

    for b in bindings.values():
        spawn(b, initial_rebuild=False)

    last_reg_mtime = vaults.registry_mtime()

    def sync_registry() -> None:
        nonlocal last_reg_mtime
        try:
            _def, latest = vaults.load_bindings()
        except (OSError, ValueError) as e:
            if verbose:
                logger.warning("[registry] reload failed (continuing): %s", e)
            return
        last_reg_mtime = vaults.registry_mtime()

        for name, (old_b, _t, _vs) in list(active.items()):
            if name not in latest:
                soft_remove(name, reason="left registry")
                continue
            new_b = latest[name]
            if str(old_b.root) != str(new_b.root) or str(old_b.index) != str(new_b.index):
                # Path identity changed — soft-remove old + hot-add new.
                soft_remove(name, reason="root/index changed")
                spawn(new_b, initial_rebuild=True)

        for name, new_b in latest.items():
            if name not in active:
                spawn(new_b, initial_rebuild=True)

    try:
        while any(t.is_alive() for _, t, _ in active.values()) and not process_stop.is_set():
            woke = deferred.wake_registry_pending()
            mt = vaults.registry_mtime()
            if woke or (mt is not None and last_reg_mtime is not None and mt > last_reg_mtime):
                sync_registry()
            elif mt is not None and last_reg_mtime is None:
                last_reg_mtime = mt
            for _b, t, _vs in list(active.values()):
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        process_stop.set()
        for _b, _t, vault_stop in list(active.values()):
            vault_stop.set()
        if verbose:
            logger.info("stopped")
        for _b, t, _vs in list(active.values()):
            t.join(timeout=5)


def _watch_one(
    binding,
    interval: float | None = None,
    *,
    use_events: bool | None = None,
    verbose: bool = True,
    stop: threading.Event | None = None,
) -> None:
    """Watch a single bound vault (caller must ``vaults.bind`` first)."""
    poll = interval if interval is not None else config.WATCH_POLL_INTERVAL
    events_on = config.WATCH_USE_EVENTS if use_events is None else use_events
    debounce_s = config.WATCH_DEBOUNCE
    root = binding.root
    collection = binding.collection
    index_path = binding.index
    label = binding.name

    # Idempotent — covers a direct `_watch_one` call (tests, future callers)
    # that skips `run_watch`'s own setup.
    _configure_logging(verbose)

    # A caller-supplied event is shared across vaults; only signal shutdown on an
    # event we own, so one vault's exit cannot stop its siblings.
    owns_stop = stop is None
    if stop is None:
        stop = threading.Event()

    debouncer = PathDebouncer(debounce_s)
    event_queue: queue.Queue[str] = queue.Queue()
    sync_ctl = git_sync.VaultSyncController(root, verbose=verbose)
    from . import optima_merge as _optima_merge

    merge_ctl = _optima_merge.VaultMergeController(root, verbose=verbose)
    hook_health = watch_health.HookHealthTracker(root)

    ignore_res = core._compile_ignore(core._load_ignore())

    def on_fs_event(raw: str) -> None:
        if _event_path_noise(raw, root, ignore_res):
            return
        p = _note_path(root, raw)
        if p is not None:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                return
            if core._is_ignored(rel, ignore_res):
                return
            debouncer.touch(p)
            event_queue.put("fs")

    observer = None
    if events_on:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        on_fs_event(event.src_path)

                def on_modified(self, event):
                    if not event.is_directory:
                        on_fs_event(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        on_fs_event(event.dest_path)
                        on_fs_event(event.src_path)

                def on_deleted(self, event):
                    if not event.is_directory:
                        on_fs_event(event.src_path)

            observer = Observer()
            observer.schedule(Handler(), str(root), recursive=True)
            observer.start()
            if verbose:
                logger.info(
                    "[%s] Watching %s (fsevents + %ss poll, debounce %ss) → %s",
                    label,
                    root,
                    poll,
                    debounce_s,
                    index_path,
                )
        except ImportError:
            observer = None
            if verbose:
                logger.info("[%s] watchdog not installed — poll-only mode", label)

    if observer is None:
        if verbose:
            logger.info(
                "[%s] Watching %s every %ss (debounce %ss) → %s",
                label,
                root,
                poll,
                debounce_s,
                index_path,
            )

    last_scan = 0.0
    lock_backoff = 0.0
    reconcile = (
        poll
        if observer is None
        else max(poll, float(getattr(config, "WATCH_RECONCILE_INTERVAL", 300.0)))
    )
    if verbose and observer is not None:
        logger.info("[%s] reconcile walk every %.0fs", label, reconcile)
    try:
        while not stop.is_set():
            woke = deferred.wake_pending(collection)
            while True:
                try:
                    event_queue.get_nowait()
                    woke = True
                except queue.Empty:
                    break

            now = time.monotonic()
            due_poll = observer is None or (now - last_scan) >= reconcile

            try:
                apo_write_batch = False
                if woke or due_poll:
                    for raw in deferred.consume_index_queue(collection):
                        apo_write_batch = True
                        p = _note_path(root, raw)
                        if p is None:
                            try:
                                cand = Path(raw).resolve()
                                cand.relative_to(root)
                                p = cand if is_note_path(cand) else None
                            except (OSError, ValueError):
                                p = None
                        if p is not None:
                            debouncer.touch(p, now=now)

                    if apo_write_batch:
                        sync_ctl.note_apo_writes()

                    stats = core.process_queues(
                        collection,
                        scan_vault=due_poll,
                        consume_index=False,
                        verbose=verbose,
                    )
                else:
                    stats = core.QueueStats()

                now = time.monotonic()
                ready = debouncer.ready(now=now)
                if ready:
                    stats.indexed += _index_paths(ready, verbose=verbose)
                    try:
                        from . import vault_project

                        if any(
                            vault_project.is_contracts_rel(
                                p.relative_to(root).as_posix()
                            )
                            for p in ready
                        ):
                            vault_project.maybe_reproject(
                                reason=f"contracts:{label}", verbose=verbose
                            )
                    except Exception as proj_err:
                        if verbose:
                            logger.warning(
                                "[%s] desk-project error: %s", label, proj_err
                            )

                # Desk overlay lives outside vault roots — poll mtime each cycle.
                # Also doubles as the desk-reprojection hook's heartbeat: it runs
                # unconditionally every cycle, unlike the contracts-triggered call
                # above, so its tick timing is what `watch-hooks.json` tracks.
                _t0 = time.monotonic()
                try:
                    from . import vault_project

                    vault_project.maybe_reproject(reason="desk-poll", verbose=verbose)
                except Exception as proj_err:
                    hook_health.record(
                        watch_health.DESK_REPROJECT,
                        seconds=time.monotonic() - _t0,
                        ok=False,
                        error=str(proj_err),
                    )
                    if verbose:
                        logger.warning(
                            "[%s] desk-poll project error: %s", label, proj_err
                        )
                else:
                    hook_health.record(
                        watch_health.DESK_REPROJECT,
                        seconds=time.monotonic() - _t0,
                        ok=True,
                    )

                if verbose:
                    summary = _format_indexed_summary(stats, ready, root)
                    if summary is not None:
                        logger.info("[%s] indexed: %s", label, summary)

                _t0 = time.monotonic()
                try:
                    sync_ctl.tick(index_busy=debouncer.waiting() > 0)
                except Exception as sync_err:
                    hook_health.record(
                        watch_health.GIT_SYNC,
                        seconds=time.monotonic() - _t0,
                        ok=False,
                        error=str(sync_err),
                    )
                    if verbose:
                        logger.warning("[%s] git-sync tick error: %s", label, sync_err)
                else:
                    hook_health.record(
                        watch_health.GIT_SYNC, seconds=time.monotonic() - _t0, ok=True
                    )

                _t0 = time.monotonic()
                try:
                    merge_ctl.tick(index_busy=debouncer.waiting() > 0)
                except Exception as merge_err:
                    hook_health.record(
                        watch_health.OPTIMA_MERGE,
                        seconds=time.monotonic() - _t0,
                        ok=False,
                        error=str(merge_err),
                    )
                    if verbose:
                        logger.warning(
                            "[%s] optima-merge tick error: %s", label, merge_err
                        )
                else:
                    hook_health.record(
                        watch_health.OPTIMA_MERGE,
                        seconds=time.monotonic() - _t0,
                        ok=True,
                    )

                if due_poll:
                    last_scan = now
                core.clear_index_lock_health()
                core.checkpoint_wal_if_over_limit(verbose=verbose)
                lock_backoff = 0.0
            except Exception as e:
                if isinstance(e, sqlite3.Error):
                    core.writer_reset()
                    msg = str(e).lower()
                    if isinstance(e, sqlite3.OperationalError) and (
                        "locked" in msg or "busy" in msg
                    ):
                        lock_backoff = core.note_index_lock_error()
                    else:
                        lock_backoff = config.WATCH_LOCK_BACKOFF_START
                if verbose:
                    suffix = f" (backing off {lock_backoff:.0f}s)" if lock_backoff else ""
                    logger.error(
                        "[%s] watch cycle error (continuing)%s: %s", label, suffix, e
                    )

            due_in = debouncer.next_due_in()
            if lock_backoff > 0:
                timeout = max(0.05, lock_backoff)
            elif due_in is not None:
                timeout = max(0.05, min(due_in, 1.0 if observer is not None else min(poll, 5.0)))
            else:
                timeout = 1.0 if observer is not None else min(poll, 5.0)
            try:
                event_queue.get(timeout=timeout)
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        if verbose:
            logger.info("[%s] stopped", label)
    finally:
        if owns_stop:
            stop.set()
        core.writer_close()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
