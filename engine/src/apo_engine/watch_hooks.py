"""Non-blocking workers for watcher peripheral hooks."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .watch_health import HookHealthTracker

logger = logging.getLogger("apo.watch")


class HookWorker:
    """Run one hook serially and coalesce triggers received while it runs."""

    def __init__(
        self,
        name: str,
        hook: Callable[..., Any],
        health: HookHealthTracker,
        *,
        verbose: bool = True,
    ) -> None:
        self.name = name
        self._hook = hook
        self._health = health
        self._verbose = verbose
        self._condition = threading.Condition()
        self._pending: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"apo-watch-hook-{name}",
            daemon=True,
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def trigger(self, *args: Any, **kwargs: Any) -> bool:
        """Queue work without waiting. The latest trigger replaces pending work."""
        with self._condition:
            if self._stopping:
                return False
            self._pending = (args, kwargs)
            self._condition.notify()
        return True

    def stop(self, timeout: float = 1.0) -> bool:
        """Request shutdown and wait at most ``timeout`` seconds."""
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify()
        self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                args, kwargs = self._pending
                self._pending = None

            started = time.monotonic()
            self._health.record_started(self.name)
            try:
                result = self._hook(*args, **kwargs)
            except Exception as exc:
                self._health.record(
                    self.name,
                    seconds=time.monotonic() - started,
                    ok=False,
                    error=str(exc),
                )
                if self._verbose:
                    logger.warning("[%s] hook error: %s", self.name, exc)
            else:
                if result is None:
                    self._health.record_noop(
                        self.name,
                        seconds=time.monotonic() - started,
                    )
                    continue
                if isinstance(result, Mapping) and result.get("ok") is False:
                    error = str(
                        result.get("message")
                        or result.get("error")
                        or "hook returned ok=false"
                    )
                    self._health.record(
                        self.name,
                        seconds=time.monotonic() - started,
                        ok=False,
                        error=error,
                    )
                    if self._verbose:
                        logger.warning("[%s] hook failed: %s", self.name, error)
                    continue
                self._health.record(
                    self.name,
                    seconds=time.monotonic() - started,
                    ok=True,
                )


class WatchHooks:
    """Own one persistent daemon worker for each registered watcher hook."""

    def __init__(self, health: HookHealthTracker, *, verbose: bool = True) -> None:
        self._health = health
        self._verbose = verbose
        self._workers: dict[str, HookWorker] = {}

    def register(self, name: str, hook: Callable[..., Any]) -> None:
        if name in self._workers:
            raise ValueError(f"watch hook already registered: {name}")
        self._workers[name] = HookWorker(name, hook, self._health, verbose=self._verbose)

    def trigger(self, name: str, *args: Any, **kwargs: Any) -> bool:
        worker = self._workers.get(name)
        return worker.trigger(*args, **kwargs) if worker is not None else False

    def trigger_all(self, *args: Any, **kwargs: Any) -> None:
        for worker in self._workers.values():
            worker.trigger(*args, **kwargs)

    def shutdown(self, timeout: float = 5.0) -> list[str]:
        """Stop all workers within one shared deadline. Return workers still alive."""
        deadline = time.monotonic() + max(0.0, timeout)
        alive: list[str] = []
        for name, worker in self._workers.items():
            remaining = max(0.0, deadline - time.monotonic())
            if not worker.stop(remaining):
                alive.append(name)
        return alive
