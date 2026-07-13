"""Cross-process index work queues — MCP enqueues, watcher consumes.

Queues live under ~/.apo/ and coordinate single-writer indexing: the MCP server
never writes to index.db; the watcher is the sole SQLite writer.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Callable

DEFERRED_DIR = Path.home() / ".apo"


def _queue_path(collection: str, kind: str) -> Path:
    return DEFERRED_DIR / f"{kind}-{collection}.json"


def _locked_update(path: Path, updater: Callable[[list], list]) -> list:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        try:
            data = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            data = []
        if not isinstance(data, list):
            data = []
        result = updater(data)
        f.seek(0)
        f.truncate()
        json.dump(result, f)
        f.flush()
        return result


def load_index_queue(collection: str) -> set[str]:
    p = _queue_path(collection, "deferred")
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def enqueue_index(collection: str, path: str) -> None:
    abs_path = str(Path(path).resolve())

    def add(items: list) -> list:
        if abs_path not in items:
            items.append(abs_path)
        return items

    _locked_update(_queue_path(collection, "deferred"), add)
    touch_wake(collection)


def enqueue_purge(collection: str, path: str) -> None:
    abs_path = str(Path(path).resolve())

    def add(items: list) -> list:
        if abs_path not in items:
            items.append(abs_path)
        return items

    _locked_update(_queue_path(collection, "purge"), add)
    touch_wake(collection)


def consume_index_queue(collection: str) -> list[str]:
    consumed: list[str] = []

    def drain(items: list) -> list:
        nonlocal consumed
        consumed = [str(x) for x in items]
        return []

    _locked_update(_queue_path(collection, "deferred"), drain)
    return consumed


def consume_purge_queue(collection: str) -> list[str]:
    consumed: list[str] = []

    def drain(items: list) -> list:
        nonlocal consumed
        consumed = [str(x) for x in items]
        return []

    _locked_update(_queue_path(collection, "purge"), drain)
    return consumed


def save_index_queue(collection: str, paths: set[str]) -> None:
    p = _queue_path(collection, "deferred")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(paths)), encoding="utf-8")
    except OSError:
        pass


def signal_rebuild(collection: str, *, force: bool = False) -> None:
    p = DEFERRED_DIR / f"rebuild-{collection}.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"force": force}), encoding="utf-8")
    except OSError:
        pass
    touch_wake(collection)


def consume_rebuild(collection: str) -> dict | None:
    p = DEFERRED_DIR / f"rebuild-{collection}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        p.unlink(missing_ok=True)
        return data if isinstance(data, dict) else {"force": False}
    except (OSError, json.JSONDecodeError):
        p.unlink(missing_ok=True)
        return None


def touch_wake(collection: str) -> None:
    try:
        DEFERRED_DIR.mkdir(parents=True, exist_ok=True)
        (DEFERRED_DIR / f"wake-{collection}").touch()
    except OSError:
        pass


def wake_pending(collection: str) -> bool:
    p = DEFERRED_DIR / f"wake-{collection}"
    if not p.is_file():
        return False
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False
