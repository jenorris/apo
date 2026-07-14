"""Vault watcher — filesystem events + deferred queue consumer (sole index writer)."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import config, core, deferred


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
    if p.suffix != ".md":
        return None
    # Deleted notes still need indexing (purge); keep non-files for deletions.
    if p.is_file() or not p.exists():
        return p
    return None


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
            print(f"  skip batch: {e}", flush=True)
        return 0


def run_watch(interval: float | None = None, *, use_events: bool | None = None, verbose: bool = True) -> None:
    """Watch vault for changes; consume deferred/purge queues; index incrementally."""
    poll = interval if interval is not None else config.WATCH_POLL_INTERVAL
    events_on = config.WATCH_USE_EVENTS if use_events is None else use_events
    debounce_s = config.WATCH_DEBOUNCE
    root = config.NOTES_ROOT.resolve()
    collection = config.COLLECTION

    debouncer = PathDebouncer(debounce_s)
    event_queue: queue.Queue[str] = queue.Queue()
    stop = threading.Event()

    def on_fs_event(raw: str) -> None:
        p = _note_path(root, raw)
        if p is not None:
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
                print(
                    f"Watching {root} (fsevents + {poll}s poll, debounce {debounce_s}s) → {config.INDEX_PATH}",
                    flush=True,
                )
        except ImportError:
            observer = None
            if verbose:
                print("watchdog not installed — poll-only mode", flush=True)

    if observer is None:
        if verbose:
            print(
                f"Watching {root} every {poll}s (debounce {debounce_s}s) → {config.INDEX_PATH}",
                flush=True,
            )

    last_scan = 0.0
    reconcile = (
        poll
        if observer is None
        else max(poll, float(getattr(config, "WATCH_RECONCILE_INTERVAL", 300.0)))
    )
    if verbose and observer is not None:
        print(f"  reconcile walk every {reconcile:.0f}s (events drive day-to-day index)", flush=True)
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

            if woke or due_poll:
                for raw in deferred.consume_index_queue(collection):
                    p = _note_path(root, raw)
                    if p is None:
                        try:
                            cand = Path(raw).resolve()
                            cand.relative_to(root)
                            p = cand if cand.suffix == ".md" else None
                        except (OSError, ValueError):
                            p = None
                    if p is not None:
                        debouncer.touch(p, now=now)

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

            if verbose and (stats.indexed or stats.purged or (
                stats.vault_stats
                and (stats.vault_stats.added or stats.vault_stats.changed or stats.vault_stats.removed)
            )):
                parts = []
                if stats.indexed:
                    parts.append(f"{stats.indexed} file(s)")
                if stats.purged:
                    parts.append(f"{stats.purged} purged")
                if stats.vault_stats and (
                    stats.vault_stats.added or stats.vault_stats.changed or stats.vault_stats.removed
                ):
                    vs = stats.vault_stats
                    parts.append(f"scan +{vs.added} ~{vs.changed} -{vs.removed}")
                print(f"  indexed: {', '.join(parts)}", flush=True)

            if due_poll:
                last_scan = now

            due_in = debouncer.next_due_in()
            if due_in is not None:
                timeout = max(0.05, min(due_in, 1.0 if observer is not None else min(poll, 5.0)))
            else:
                timeout = 1.0 if observer is not None else min(poll, 5.0)
            try:
                event_queue.get(timeout=timeout)
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        if verbose:
            print("\nstopped", flush=True)
    finally:
        stop.set()
        core.writer_close()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
