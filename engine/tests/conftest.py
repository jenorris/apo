"""Shared test isolation — no test may touch the real ~/.apo runtime directory.

Redirects deferred queues, tool-metrics DuckDB, and the watcher PID probe into a
per-test tmp dir. Applies to unittest.TestCase tests too (autouse side effects).
"""
from __future__ import annotations

import pytest

from apo_engine import deferred, ops, tool_metrics, vaults


@pytest.fixture(autouse=True)
def _isolated_apo_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "apo-runtime"
    monkeypatch.setattr(deferred, "DEFERRED_DIR", runtime)
    monkeypatch.setattr(tool_metrics, "DEFERRED_DIR", runtime)
    # Deterministic watcher state (missing pid file → not running) regardless of host.
    monkeypatch.setattr(ops, "WATCH_PID_FILE", runtime / "watch.pid")
    monkeypatch.setenv("APO_DEFERRED_DIR", str(runtime))
    # A dev shell exporting APO_VAULTS must not leak real vaults into tests
    # (tests that need a registry set it explicitly).
    for key in (
        "APO_VAULTS",
        "APO_VAULT_PATHS",
        "APO_VAULT_PATH_LIST",
        "APO_COLLECTION_ROOT",
        "APO_DEFAULT_VAULT",
    ):
        monkeypatch.delenv(key, raising=False)
    # Non-git vault roots in tests (plain tempdirs) hit compute_vault_id's
    # fallback path — must not write into the real ~/.apo/vault-ids.json.
    monkeypatch.setattr(vaults, "_FALLBACK_ID_STORE", runtime / "vault-ids.json")
    vaults._vault_id_cache.clear()
