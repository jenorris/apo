from __future__ import annotations

import tempfile
import threading
import time
import inspect
from pathlib import Path

from apo_engine import watch, watch_health
from apo_engine.watch_hooks import WatchHooks


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _hooks() -> WatchHooks:
    root = Path(tempfile.mkdtemp(prefix="apo-watch-hooks-"))
    return WatchHooks(watch_health.HookHealthTracker(root), verbose=False)


def test_trigger_never_blocks_on_running_hook():
    hooks = _hooks()
    release = threading.Event()
    hooks.register("slow", lambda: release.wait(5))
    started = time.monotonic()
    assert hooks.trigger("slow")
    assert time.monotonic() - started < 0.1
    release.set()
    assert hooks.shutdown() == []


def test_repeated_triggers_coalesce_to_one_rerun():
    hooks = _hooks()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def job(value: int) -> None:
        calls.append(value)
        entered.set()
        if len(calls) == 1:
            release.wait(2)

    hooks.register("job", job)
    hooks.trigger("job", 1)
    assert entered.wait(1)
    hooks.trigger("job", 2)
    hooks.trigger("job", 3)
    release.set()
    _wait_for(lambda: len(calls) == 2)
    assert calls == [1, 3]
    assert hooks.shutdown() == []


def test_exception_does_not_kill_worker():
    hooks = _hooks()
    recovered = threading.Event()
    calls = 0

    def job() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        recovered.set()

    hooks.register("job", job)
    hooks.trigger("job")
    _wait_for(lambda: calls == 1)
    hooks.trigger("job")
    assert recovered.wait(1)
    assert hooks.shutdown() == []


def test_shutdown_is_bounded_when_hook_hangs():
    hooks = _hooks()
    entered = threading.Event()
    release = threading.Event()

    def job() -> None:
        entered.set()
        release.wait(5)

    hooks.register("hung", job)
    hooks.trigger("hung")
    assert entered.wait(1)
    started = time.monotonic()
    assert hooks.shutdown(timeout=0.05) == ["hung"]
    assert time.monotonic() - started < 0.2
    release.set()


def test_health_exposes_running_and_start_state():
    root = Path(tempfile.mkdtemp(prefix="apo-watch-hooks-health-"))
    hooks = WatchHooks(watch_health.HookHealthTracker(root), verbose=False)
    entered = threading.Event()
    release = threading.Event()

    def job() -> None:
        entered.set()
        release.wait(2)

    hooks.register("job", job)
    hooks.trigger("job")
    assert entered.wait(1)
    row = watch_health.read_hook_health(root)["job"]
    assert row["running"] is True
    assert row["started_at"]
    release.set()
    assert hooks.shutdown() == []


def test_generic_watch_module_has_no_optima_dependency():
    assert "optima" not in inspect.getsource(watch).lower()
