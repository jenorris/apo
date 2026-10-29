"""Watcher: OKF contract edits trigger a links backfill without waiting for a reconcile scan.

Contract files match ``DEFAULT_YAML_IGNORE``, so the regular FS-event path drops
them before debounce; ``_contracts_event_rel`` flags them first.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import config, core, deferred, vaults, watch
from apo_engine.okf import contract as okf_contract
from test_retrieval_gaps import _fake_embed

_CONTRACT = "system/contracts/okf-contract.schema.yaml"


class ContractsEventRelTest(unittest.TestCase):
    def setUp(self):
        # macOS: tempfile hands out /var/…, fsevents reports /private/var/….
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_contract_file_is_flagged_although_ignore_listed(self):
        raw = str(self.root / _CONTRACT)
        ignore_res = core._compile_ignore(core._load_ignore())
        self.assertTrue(watch._event_path_noise(raw, self.root, ignore_res))  # the bug's premise
        self.assertEqual(watch._contracts_event_rel(raw, self.root), _CONTRACT)

    def test_resolved_root_path_matches(self):
        raw = str(self.root.resolve() / _CONTRACT)
        self.assertEqual(watch._contracts_event_rel(raw, self.root), _CONTRACT)

    def test_other_paths_are_not_contract_events(self):
        self.assertIsNone(watch._contracts_event_rel(str(self.root / "notes/a.md"), self.root))
        self.assertIsNone(watch._contracts_event_rel("/elsewhere/system/contracts/x.yaml", self.root))
        self.assertIsNone(watch._contracts_event_rel("", self.root))


class ContractTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "svc.md").write_text("---\ndepends_on: [db.md]\n---\n\n# svc\n", encoding="utf-8")
        (self.vault / "db.md").write_text("# db\n", encoding="utf-8")
        self.index = self.tmp / "index.db"
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "watch_contracts_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
            mock.patch.object(deferred, "DEFERRED_DIR", self.runtime),
            mock.patch.dict(os.environ, {"APO_WATCH_LOG_FILE": str(self.runtime / "watch.log")}),
        ]
        for patch in self._patches:
            patch.start()
        okf_contract.clear_contract_cache()

    def tearDown(self):
        core.writer_close()
        core.reader_close()
        for patch in self._patches:
            patch.stop()
        okf_contract.clear_contract_cache()
        core._schema_ready.discard(str(self.index.resolve()))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_contract(self, data: dict) -> None:
        (self.vault / _CONTRACT).write_text(yaml.safe_dump(data), encoding="utf-8")

    def stored_state(self) -> str | None:
        try:
            db = sqlite3.connect(f"file:{self.index}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = db.execute("SELECT value FROM meta WHERE key='links_schema_version'").fetchone()
        except sqlite3.Error:
            return None
        finally:
            db.close()
        return row[0] if row else None

    def wait_for(self, pred, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.1)
        return False

    def test_backfill_helper_runs_only_when_state_is_stale(self):
        core.index_vault(verbose=False)
        self.assertFalse(watch._backfill_links_if_needed("t", verbose=False))
        self.write_contract({"link_fields": [{"key": "depends_on", "kind": "depends-on"}]})
        self.assertTrue(watch._backfill_links_if_needed("t", verbose=False))
        self.assertEqual({r["kind"] for r in core.list_links(source="svc.md")}, {"depends-on"})
        self.assertFalse(watch._backfill_links_if_needed("t", verbose=False))

    def test_contract_edit_backfills_before_reconcile(self):
        try:
            import watchdog  # noqa: F401
        except ImportError:
            self.skipTest("watchdog not installed")
        binding = vaults.VaultBinding(
            name="watch_contracts_test", root=self.vault, index=self.index, collection="watch_contracts_test"
        )
        stop = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                with vaults.bind(binding):
                    watch._watch_one(binding, interval=0.2, use_events=True, verbose=False, stop=stop)
            except BaseException as e:  # surfaced below
                errors.append(e)

        with mock.patch.object(config, "WATCH_RECONCILE_INTERVAL", 3600.0), \
             mock.patch.object(config, "WATCH_DEBOUNCE", 0.2):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            try:
                # First loop iteration runs the startup scan and stamps the links state.
                self.assertTrue(self.wait_for(lambda: self.stored_state() is not None, 30), errors)
                before = self.stored_state()
                time.sleep(1.0)  # let the observer settle
                self.write_contract({"link_fields": [{"key": "depends_on", "kind": "depends-on"}]})
                # Reconcile is an hour away — only the contract event path can do this.
                self.assertTrue(
                    self.wait_for(lambda: self.stored_state() not in (None, before), 20),
                    f"no links backfill after contract edit; errors={errors}",
                )
            finally:
                stop.set()
                t.join(timeout=10)
        self.assertFalse(errors, errors)
        kinds = {r[0] for r in sqlite3.connect(f"file:{self.index}?mode=ro", uri=True).execute(
            "SELECT kind FROM backlinks WHERE source='svc.md'")}
        self.assertEqual(kinds, {"depends-on"})


if __name__ == "__main__":
    unittest.main()
