"""Vault watcher — filesystem events + deferred queue consumer (sole index writer)."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import config, core, deferred


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
    if p.suffix != ".md" or not p.is_file():
        return None
    return p


def _index_paths(paths: set[Path], *, verbose: bool) -> int:
    indexed = 0
    for p in sorted(paths):
        try:
            core.index_file(p, verbose=verbose)
            indexed += 1
        except (OSError, ValueError) as e:
            if verbose:
                print(f"  skip {p}: {e}", flush=True)
    return indexed


def run_watch(interval: float | None = None, *, use_events: bool | None = None, verbose: bool = True) -> None:
    """Watch vault for changes; consume deferred/purge queues; index incrementally."""
    poll = interval if interval is not None else config.WATCH_POLL_INTERVAL
    events_on = config.WATCH_USE_EVENTS if use_events is None else use_events
    root = config.NOTES_ROOT.resolve()
    collection = config.COLLECTION

    pending: set[Path] = set()
    event_queue: queue.Queue[str] = queue.Queue()
    stop = threading.Event()

    def on_fs_event(raw: str) -> None:
        p = _note_path(root, raw)
        if p is not None:
            pending.add(p)
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
                print(f"Watching {root} (fsevents + {poll}s poll) → {config.INDEX_PATH}", flush=True)
        except ImportError:
            observer = None
            if verbose:
                print("watchdog not installed — poll-only mode", flush=True)

    if observer is None:
        if verbose:
            print(f"Watching {root} every {poll}s → {config.INDEX_PATH}", flush=True)

    last_scan = 0.0
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
            due_poll = observer is None or (now - last_scan) >= poll

            if woke or due_poll:
                stats = core.process_queues(collection, scan_vault=due_poll, verbose=verbose)
                if pending:
                    extra = _index_paths(pending, verbose=verbose)
                    stats.indexed += extra
                    pending.clear()
                if verbose and (stats.indexed or stats.purged or stats.vault_stats):
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
                last_scan = now

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
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
