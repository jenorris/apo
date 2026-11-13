from __future__ import annotations

import tempfile
import threading
import time
import inspect
from pathlib import Path
from unittest import mock

import pytest

from apo_engine import optima_merge, watch, watch_health
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


@pytest.mark.parametrize(
    ("merge_effect", "message"),
    [
        (RuntimeError("source exploded"), "source exploded"),
        ({"ok": False, "message": "source contract invalid"}, "source contract invalid"),
    ],
)
def test_optima_merge_failures_persist_and_noop_does_not_clear(
    merge_effect: object, message: str
) -> None:
    root = Path(tempfile.mkdtemp(prefix="apo-watch-hooks-optima-"))
    health = watch_health.HookHealthTracker(root)
    hooks = WatchHooks(health, verbose=False)
    controller = optima_merge.VaultMergeController(root, verbose=False)
    controller._last_merge_at = time.monotonic() - 10

    with (
        mock.patch.object(optima_merge.optima_contract, "merge_enabled", return_value=True),
        mock.patch.object(
            optima_merge.optima_contract,
            "merge_settings",
            return_value=mock.Mock(interval_seconds=1),
        ),
        mock.patch.object(
            optima_merge,
            "run_merge",
            side_effect=merge_effect if isinstance(merge_effect, Exception) else None,
            return_value=merge_effect if isinstance(merge_effect, dict) else None,
        ),
    ):
        hooks.register(
            watch_health.OPTIMA_MERGE,
            lambda: controller.tick(index_busy=False),
        )
        assert hooks.trigger(watch_health.OPTIMA_MERGE)
        _wait_for(
            lambda: f"hook_error:{watch_health.OPTIMA_MERGE}"
            in watch_health.hook_health_flags(watch_health.read_hook_health(root))
        )
        failed = watch_health.read_hook_health(root)[watch_health.OPTIMA_MERGE]
        assert failed["error"] == message

        # The merge interval now makes this trigger a no-op. It must clear
        # running state without replacing the persisted failed completion.
        assert hooks.trigger(watch_health.OPTIMA_MERGE)
        _wait_for(
            lambda: not watch_health.read_hook_health(root)[
                watch_health.OPTIMA_MERGE
            ].get("running", False)
        )
        later = watch_health.read_hook_health(root)[watch_health.OPTIMA_MERGE]
        assert later["ok"] is False
        assert later["error"] == message

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
