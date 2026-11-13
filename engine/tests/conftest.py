"""Shared test isolation — no test may touch the real ~/.apo runtime directory.

Redirects deferred queues, tool-metrics DuckDB, and the watcher PID probe into a
per-test tmp dir. Applies to unittest.TestCase tests too (autouse side effects).
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Session-wide fake HOME, set BEFORE apo_engine is imported. Several modules
# capture ``Path.home() / ".apo"`` at import time (vaults._FALLBACK_ID_STORE,
# metrics_backend.DEFAULT_EMBEDDED_PATH, deferred/tool_metrics DEFERRED_DIR), and
# some tests re-import apo_engine from scratch (test_mcp_server_vault_registry
# purges sys.modules), which re-evaluates those constants against the *real*
# home. A per-test monkeypatch cannot cover that; redirecting HOME can.
REAL_HOME = Path.home()
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="apo-test-home-")).resolve()
atexit.register(shutil.rmtree, _SESSION_HOME, True)
os.environ["HOME"] = str(_SESSION_HOME)
os.environ["USERPROFILE"] = str(_SESSION_HOME)
# config.INDEX_PATH / NOTES_ROOT default to engine/index.db and ~/Notes when the
# env vars are unset — tests that never configure a vault would otherwise create
# engine/index.db in the source tree or read the developer's real notes.
os.environ["APO_INDEX"] = str(_SESSION_HOME / "default-index.db")
os.environ["APO_NOTES_ROOT"] = str(_SESSION_HOME / "default-notes")

import pytest  # noqa: E402

from apo_engine import (  # noqa: E402
    config,
    deferred,
    ops,
    otel_forwarder,
    tool_metrics,
    vaults,
)


@pytest.fixture(autouse=True)
def _isolated_apo_runtime(tmp_path, tmp_path_factory, monkeypatch):
    runtime = tmp_path / "apo-runtime"
    monkeypatch.setattr(deferred, "DEFERRED_DIR", runtime)
    monkeypatch.setattr(tool_metrics, "DEFERRED_DIR", runtime)
    # Deterministic watcher state (missing pid file → not running) regardless of host.
    monkeypatch.setattr(ops, "WATCH_PID_FILE", runtime / "watch.pid")
    monkeypatch.setenv("APO_DEFERRED_DIR", str(runtime))
    # watch.py's `_configure_logging` defaults its rotating log file under
    # `WATCH_PID_DIR` (falling back to `~/.apo`) — any test that exercises
    # `run_watch`/`_watch_one` (even with `_watch_one` mocked out) must not
    # open/rotate the real `~/.apo/watch.log`.
    monkeypatch.setenv("WATCH_PID_DIR", str(runtime))
    # A dev shell exporting APO_VAULTS must not leak real vaults into tests
    # (tests that need a registry set it explicitly).
    for key in (
        "APO_VAULTS",
        "APO_VAULT_PATHS",
        "APO_COLLECTION_ROOT",
        "APO_DEFAULT_VAULT",
    ):
        monkeypatch.delenv(key, raising=False)
    # Non-git vault roots in tests (plain tempdirs) hit compute_collection_id's
    # fallback path — must not write into the real ~/.apo/vault-ids.json.
    monkeypatch.setattr(vaults, "_FALLBACK_ID_STORE", runtime / "vault-ids.json")
    # Per-test default index + notes root (see session HOME block above). A fresh
    # path per test also avoids stale ``_schema_ready`` entries for a shared file.
    # The index dir is a sibling of ``tmp_path`` (not inside it) so tests that assert
    # on the exact contents of ``tmp_path`` still see what they created. The notes
    # root is deliberately never created: it only has to not be ``~/Notes``.
    defaults = tmp_path_factory.mktemp("apo-defaults")
    monkeypatch.setattr(config, "INDEX_PATH", defaults / "index.db")
    monkeypatch.setattr(config, "NOTES_ROOT", defaults / "notes")
    vaults._vault_id_cache.clear()
    # load_bindings() is cached (perf fix) — same leak risk as _vault_id_cache above.
    vaults.invalidate_bindings_cache()
    # read_usage_layout() is cached too — same leak risk, same fix.
    vaults._usage_layout_cache.clear()
    yield
    # Queued metrics events must land (or be dropped) before the runtime redirect is undone.
    tool_metrics.flush()
    otel_forwarder.flush()
