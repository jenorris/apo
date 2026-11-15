from __future__ import annotations

import inspect
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import pytest

from apo_engine import config, core, deferred, vaults, watch, watch_health
from apo_engine.index_models import QueueStats
from apo_engine import watch_hooks as watch_hooks_module
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
    assert "optima" not in inspect.getsource(watch_hooks_module).lower()


def _fake_entry_point(name: str, loader):
    ep = mock.Mock()
    ep.name = name
    ep.load.side_effect = loader
    return ep


def test_no_entry_points_means_no_plugin_hooks():
    with mock.patch.object(watch_hooks_module.metadata, "entry_points", return_value=[]) as eps:
        assert watch_hooks_module.load_entry_point_factories() == ()
    eps.assert_called_once_with(group="apo.watch_hooks")


def test_entry_point_factories_load_and_a_broken_one_is_skipped():
    def good(binding, verbose):
        return (("plug", lambda **kw: None),)

    def boom():
        raise ImportError("missing dependency")

    with mock.patch.object(
        watch_hooks_module.metadata,
        "entry_points",
        return_value=[_fake_entry_point("bad", boom), _fake_entry_point("good", lambda: good)],
    ):
        assert watch_hooks_module.load_entry_point_factories() == (good,)


def _watch_one_registrations(hook_factories) -> list[str]:
    """Run `_watch_one` for one idle cycle; return the hook names it registered."""
    pytest.importorskip("watchdog")
    root = Path(tempfile.mkdtemp(prefix="apo-watch-reg-"))
    binding = vaults.VaultBinding("reg", root, root / "reg.db", "reg")
    stop = threading.Event()
    names: list[str] = []

    class FakeQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            if block and timeout is not None:
                stop.set()
                raise queue.Empty
            return super().get(block, timeout)

    class FakeHooks:
        def __init__(self, *a, **k) -> None: ...
        def register(self, name, hook) -> None:
            names.append(name)

        def shutdown(self, timeout=5.0) -> list[str]:
            return []

        def trigger_all(self, **kw) -> None: ...

    with (
        mock.patch.object(watch, "queue", SimpleNamespace(Queue=FakeQueue, Empty=queue.Empty)),
        mock.patch.object(watch.watch_hooks, "WatchHooks", FakeHooks),
        mock.patch("watchdog.observers.Observer"),
        mock.patch.object(deferred, "wake_pending", lambda _c: False),
        mock.patch.object(core, "process_queues", return_value=QueueStats()),
        mock.patch.object(core, "_compile_ignore", return_value=[]),
        mock.patch.object(core, "_load_ignore", return_value=[]),
        mock.patch.object(core, "clear_index_lock_health"),
        mock.patch.object(core, "checkpoint_wal_if_over_limit"),
        mock.patch.object(core, "writer_close"),
    ):
        watch._watch_one(
            binding, use_events=True, verbose=False, stop=stop, hook_factories=hook_factories
        )
    return names


def test_watch_one_registers_factory_hooks_and_survives_a_raising_factory():
    def raising(binding, verbose):
        raise RuntimeError("factory exploded")

    def fine(binding, verbose):
        return (("plug", lambda **kw: None),)

    names = _watch_one_registrations((raising, fine))
    assert names == [watch_health.DESK_REPROJECT, watch_health.GIT_SYNC, "plug"]


def test_watch_one_with_no_factories_registers_only_builtin_hooks():
    assert _watch_one_registrations(()) == [watch_health.DESK_REPROJECT, watch_health.GIT_SYNC]


def test_run_watch_passes_entry_point_factories_to_the_vault_watcher():
    def plug(binding, verbose):
        return ()

    root = Path(tempfile.mkdtemp(prefix="apo-run-watch-"))
    binding = vaults.VaultBinding("solo", root, root / "solo.db", "solo")
    with (
        mock.patch.object(watch, "_acquire_watch_lock", return_value=True),
        mock.patch.object(watch_hooks_module, "load_entry_point_factories", return_value=(plug,)),
        mock.patch.object(vaults, "load_bindings", return_value=("solo", {"solo": binding})),
        mock.patch.object(vaults, "discovery_active", return_value=False),
        mock.patch.object(watch, "_watch_one") as one,
    ):
        watch.run_watch(verbose=False)
    assert one.call_args.kwargs["hook_factories"] == (plug,)


def _run_idle_watch_cycles(
    cycles: int, *, wake_on_cycle: int | None = None
) -> list[tuple[float, int]]:
    """Drive `_watch_one` on a fake 1 s clock; return (time, cycle) per trigger_all."""
    pytest.importorskip("watchdog")
    root = Path(tempfile.mkdtemp(prefix="apo-watch-idle-"))
    binding = vaults.VaultBinding("idle", root, root / "idle.db", "idle")
    stop = threading.Event()
    clock = [10_000.0]
    cycle = [0]
    triggers: list[tuple[float, int]] = []

    class FakeQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            if block and timeout is not None:  # the loop's idle wait
                clock[0] += timeout
                cycle[0] += 1
                if cycle[0] >= cycles:
                    stop.set()
                raise queue.Empty
            return super().get(block, timeout)

    class FakeHooks:
        def __init__(self, *a, **k) -> None: ...
        def register(self, *a) -> None: ...
        def shutdown(self, timeout=5.0) -> list[str]:
            return []

        def trigger_all(self, **kw) -> None:
            triggers.append((clock[0], cycle[0]))

    def wake_pending(_collection) -> bool:
        return wake_on_cycle is not None and cycle[0] == wake_on_cycle

    with (
        mock.patch.object(watch, "time", SimpleNamespace(monotonic=lambda: clock[0])),
        mock.patch.object(watch, "queue", SimpleNamespace(Queue=FakeQueue, Empty=queue.Empty)),
        mock.patch.object(watch.watch_hooks, "WatchHooks", FakeHooks),
        mock.patch("watchdog.observers.Observer"),
        mock.patch.object(config, "WATCH_RECONCILE_INTERVAL", 3600.0),
        mock.patch.object(deferred, "wake_pending", wake_pending),
        mock.patch.object(core, "process_queues", return_value=QueueStats()),
        mock.patch.object(core, "_compile_ignore", return_value=[]),
        mock.patch.object(core, "_load_ignore", return_value=[]),
        mock.patch.object(core, "clear_index_lock_health"),
        mock.patch.object(core, "checkpoint_wal_if_over_limit"),
        mock.patch.object(core, "writer_close"),
    ):
        watch._watch_one(binding, use_events=True, verbose=False, stop=stop)
    return triggers


def test_idle_watch_loop_triggers_hooks_at_most_once_per_heartbeat():
    seconds = 60
    triggers = _run_idle_watch_cycles(seconds)
    times = [t for t, _ in triggers]
    # First cycle (startup scan) triggers; every later idle trigger is >= one heartbeat apart.
    assert len(times) >= 2
    assert all(b - a >= watch.HOOK_HEARTBEAT_S for a, b in zip(times, times[1:]))
    assert len(times) <= seconds / watch.HOOK_HEARTBEAT_S + 1
    # ...and a heartbeat still lands, so a due commit or desk poll cannot starve.
    assert times[-1] - times[0] >= seconds - watch.HOOK_HEARTBEAT_S - 1


def test_watch_work_cycle_triggers_hooks_immediately():
    triggers = _run_idle_watch_cycles(20, wake_on_cycle=7)
    # A woken cycle is a work cycle: the trigger fires even though the previous one was <5 s ago.
    cycles = [c for _, c in triggers]
    assert 7 in cycles
    assert cycles[0] == 0
