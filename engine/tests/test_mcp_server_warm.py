"""MCP cold start: the query-embed warm-up must not delay mcp.run()."""

from __future__ import annotations

import threading
from unittest import mock

from apo_engine.mcp import server


def _main_with(transport: str | None, warm) -> list[str]:
    order: list[str] = []
    env = {"APO_MCP_TRANSPORT": transport} if transport else {}

    def fake_run(*a, **k):
        order.append("run")

    with (
        mock.patch.dict("os.environ", env, clear=False),
        mock.patch.object(server, "_maybe_warm_query_embed", warm),
        mock.patch.object(server, "_require_safe_http_bind"),
        mock.patch.object(server.mcp, "run", side_effect=fake_run),
    ):
        server.main()
    return order


def test_main_runs_server_without_waiting_for_warm():
    release = threading.Event()
    warm_thread: list[threading.Thread] = []

    def slow_warm():
        warm_thread.append(threading.current_thread())
        release.wait(timeout=5)

    try:
        order = _main_with(None, slow_warm)
        assert order == ["run"]  # run() returned while warm-up is still blocked
    finally:
        release.set()
    assert warm_thread[0] is not threading.main_thread()
    assert warm_thread[0].daemon


def test_http_transport_also_warms_in_background():
    ran = threading.Event()
    assert _main_with("http", ran.set) == ["run"]
    assert ran.wait(timeout=5)


def test_warm_failure_is_best_effort():
    # _maybe_warm_query_embed swallows backend errors; a thread must never raise into main().
    from apo_engine import core

    with (
        mock.patch.object(server, "VAULTS", {"v": mock.Mock()}),
        mock.patch.object(server, "DEFAULT_VAULT", "v"),
        mock.patch.object(server, "_bound"),
        mock.patch.object(core, "warm_query_embed", side_effect=RuntimeError("down")),
    ):
        server._start_query_embed_warm().join(timeout=5)
