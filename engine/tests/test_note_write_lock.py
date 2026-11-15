"""Note write lock: flock across stat -> read -> replace for one canonical note path.

The race tests force the lost-update window open. A wrapper rendezvous two threads
right after the precondition check (or the read). Without the lock both threads reach
the rendezvous and both write. With the lock the second thread cannot enter, the
first thread's wait times out, and the second thread then sees the new mtime.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from apo_engine import atomic_io, config, ops

_RENDEZVOUS_S = 0.4


class _Rendezvous:
    """Wrap ``fn`` so each caller waits for a partner after ``fn`` returns."""

    def __init__(self, fn, barrier: threading.Barrier | None = None):
        self.fn = fn
        self.barrier = barrier or threading.Barrier(2, timeout=_RENDEZVOUS_S)

    def __call__(self, *args, **kwargs):
        result = self.fn(*args, **kwargs)
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return result


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "areas").mkdir(parents=True)
    monkeypatch.setattr(config, "NOTES_ROOT", root)
    monkeypatch.setattr(config, "INDEX_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "COLLECTION", "write_lock_test")
    monkeypatch.setattr(config, "OKF_CONTRACT", "")
    monkeypatch.setattr(config, "OKF_ENFORCEMENT", "off")
    return root


def _seed(root: Path, rel: str, text: str) -> float:
    full = root / rel
    full.write_text(text, encoding="utf-8")
    old = time.time() - 100
    os.utime(full, (old, old))
    return full.stat().st_mtime


def _run_two(fn_a, fn_b) -> list:
    results: list = [None, None]

    def runner(i, fn):
        results[i] = fn()

    threads = [
        threading.Thread(target=runner, args=(0, fn_a)),
        threading.Thread(target=runner, args=(1, fn_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    return results


def test_two_writers_same_expected_mtime_one_wins(vault, monkeypatch):
    mtime = _seed(vault, "areas/n.md", "# N\n\nold\n")
    gate = _Rendezvous(ops._guard_write)
    monkeypatch.setattr(ops, "_guard_write", gate)

    results = _run_two(
        lambda: ops.write_note("areas/n.md", "# N\n\nfrom A\n", expected_mtime=mtime),
        lambda: ops.write_note("areas/n.md", "# N\n\nfrom B\n", expected_mtime=mtime),
    )

    wins = [r for r in results if r.get("ok")]
    stale = [r for r in results if r.get("error") == "stale_write"]
    assert len(wins) == 1, results
    assert len(stale) == 1, results
    on_disk = (vault / "areas/n.md").read_text(encoding="utf-8")
    assert ("from A" in on_disk) != ("from B" in on_disk)
    assert on_disk.strip().endswith(("from A", "from B"))


def test_patch_vs_delete_same_expected_mtime_one_wins(vault, monkeypatch):
    mtime = _seed(vault, "areas/n.md", "# N\n\nold\n")
    shared = threading.Barrier(2, timeout=_RENDEZVOUS_S)
    monkeypatch.setattr(ops, "_guard_write", _Rendezvous(ops._guard_write, shared))
    monkeypatch.setattr(ops, "_check_mtime", _Rendezvous(ops._check_mtime, shared))

    results = _run_two(
        lambda: ops.patch_note(
            "areas/n.md",
            [{"op": "append_eof", "text": "patched\n"}],
            expected_mtime=mtime,
        ),
        lambda: ops.delete_note("areas/n.md", expected_mtime=mtime),
    )

    assert sum(1 for r in results if r.get("ok")) == 1, results
    loser = next(r for r in results if not r.get("ok"))
    assert loser["error"] in {"stale_write", "not_found"}, results


def test_two_deletes_same_expected_mtime_one_wins(vault, monkeypatch):
    mtime = _seed(vault, "areas/n.md", "# N\n")
    monkeypatch.setattr(ops, "_check_mtime", _Rendezvous(ops._check_mtime))

    results = _run_two(
        lambda: ops.delete_note("areas/n.md", expected_mtime=mtime),
        lambda: ops.delete_note("areas/n.md", expected_mtime=mtime),
    )

    assert sum(1 for r in results if r.get("ok")) == 1, results
    # The loser sees a missing file (not_found) or a stale mtime; never a second ok.
    assert {r.get("error") for r in results if not r.get("ok")} <= {"not_found", "stale_write"}
    assert not (vault / "areas/n.md").exists()


def test_concurrent_appends_do_not_lose_an_update(vault, monkeypatch):
    _seed(vault, "areas/n.md", "# N\n\n- base\n")
    # Rendezvous both appenders right after they read the file.
    real_read = Path.read_text
    gate = _Rendezvous(lambda p, *a, **k: real_read(p, *a, **k))
    target = vault / "areas" / "n.md"

    def gated_read(self, *a, **k):
        if self == target.resolve() or self == target:
            return gate(self, *a, **k)
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", gated_read)

    results = _run_two(
        lambda: ops.append_note("areas/n.md", "- from A"),
        lambda: ops.append_note("areas/n.md", "- from B"),
    )

    monkeypatch.setattr(Path, "read_text", real_read)
    assert all(r.get("ok") for r in results), results
    on_disk = target.read_text(encoding="utf-8")
    assert "- from A" in on_disk
    assert "- from B" in on_disk


def test_delete_note_expected_mtime(vault):
    mtime = _seed(vault, "areas/n.md", "# N\n")

    stale = ops.delete_note("areas/n.md", expected_mtime=mtime - 5)
    assert stale["ok"] is False
    assert stale["error"] == "stale_write"
    assert (vault / "areas/n.md").exists()

    ok = ops.delete_note("areas/n.md", expected_mtime=mtime)
    assert ok["ok"] is True
    assert not (vault / "areas/n.md").exists()


def test_delete_note_without_expected_mtime_still_deletes(vault):
    _seed(vault, "areas/n.md", "# N\n")
    assert ops.delete_note("areas/n.md")["ok"] is True


def test_lock_is_held_across_the_atomic_write(vault, monkeypatch):
    """A second process-style locker blocks while atomic_write_text is mid-flight."""
    _seed(vault, "areas/n.md", "# N\n")
    full = (vault / "areas/n.md").resolve()
    inside = threading.Event()
    release = threading.Event()
    real_write = ops.atomic_write_text

    def slow_write(path, content, **kw):
        inside.set()
        assert release.wait(5)
        return real_write(path, content, **kw)

    monkeypatch.setattr(ops, "atomic_write_text", slow_write)
    writer = threading.Thread(target=lambda: ops.write_note("areas/n.md", "# N\n\nx\n"))
    writer.start()
    assert inside.wait(5)

    acquired = threading.Event()

    def contender():
        with atomic_io.note_write_lock(full):
            acquired.set()

    t = threading.Thread(target=contender)
    t.start()
    assert not acquired.wait(0.3), "lock was not held during atomic_write_text"
    release.set()
    writer.join(5)
    t.join(5)
    assert acquired.is_set()


def test_lock_is_reentrant_in_one_thread(tmp_path):
    p = tmp_path / "a.md"
    with atomic_io.note_write_lock(p):
        with atomic_io.note_write_lock(p, tmp_path / "b.md"):
            pass


def test_multi_path_lock_has_no_order_deadlock(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    errors: list[BaseException] = []

    def mover(first, second):
        try:
            for _ in range(50):
                with atomic_io.note_write_lock(first, second):
                    pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [
        threading.Thread(target=mover, args=(a, b)),
        threading.Thread(target=mover, args=(b, a)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
        assert not t.is_alive(), "deadlock"
    assert not errors


class _SlowIterDict(dict):
    """Live iteration that yields the GIL, so a concurrent insert lands mid-iteration."""

    def items(self):
        for kv in super().items():
            time.sleep(0.02)
            yield kv


def test_recent_touches_prune_does_not_race_an_insert(monkeypatch):
    touches = _SlowIterDict()
    monkeypatch.setattr(ops, "_recent_touches", touches)
    monkeypatch.setattr(ops, "_RECENT_TOUCH_TTL_S", 0.0)
    for n in range(5):
        touches[f"work:areas/{n}.md"] = ops.PathTouch(mono=0.0, mtime=1.0)
    errors: list[BaseException] = []

    def pruner():
        try:
            ops._prune_recent_touches()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def inserter():
        try:
            time.sleep(0.03)
            ops._record_path_touch("work", "areas/new.md", 2.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=pruner), threading.Thread(target=inserter)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
        assert not t.is_alive()
    assert not errors, errors
