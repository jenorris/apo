"""ops.search fans (vault, folder) pairs out on a small pool; output must equal serial."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, index_db, ops, vaults

from test_search_vaults_fanout import _fake_embed


def _serial_pairs(pairs, run_pair):
    return [run_pair(b, f) for b, f in pairs]


class SearchFanoutParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        entries = {}
        for vid in ("alpha", "beta"):
            root = self.tmp / vid
            cdir = root / "system" / "contracts"
            cdir.mkdir(parents=True)
            (cdir / "usage-contract.schema.yaml").write_text(
                f"vault_id: {vid}\n", encoding="utf-8"
            )
            for folder in ("docs", "notes"):
                (root / folder).mkdir(parents=True)
                for i in range(3):
                    (root / folder / f"{vid}-{folder}-{i}.md").write_text(
                        f"# {vid} {folder} {i}\n\nparallelzzz token {vid} {folder} "
                        + "filler " * i
                        + "\n",
                        encoding="utf-8",
                    )
            entries[vid] = {
                "root": str(root),
                "index": str(self.tmp / f"{vid}.db"),
                "collection": f"pfan_{vid}",
            }
        reg = self.tmp / "vaults.json"
        reg.write_text(json.dumps({"default": "alpha", "vaults": entries}), encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"APO_VAULTS": str(reg)}, clear=False)
        self._env.start()
        self._patches = [
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", []),
        ]
        for p in self._patches:
            p.start()
        core.clear_query_embed_cache()
        _default, bindings = vaults.load_bindings()
        self.bindings = bindings
        for name in ("alpha", "beta"):
            with vaults.bind(bindings[name]):
                core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        core.clear_query_embed_cache()
        core.writer_close()
        core.reader_close()
        ops._reset_search_fanout_pool()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _search(self, **kw):
        return ops.search(
            "parallelzzz",
            vaults=["alpha", "beta"],
            folders=["docs", "notes"],
            limit=20,
            **kw,
        )

    def test_two_vaults_two_folders_match_serial(self):
        with mock.patch.object(ops, "_run_search_pairs", _serial_pairs):
            serial = self._search()
        parallel = self._search()
        self.assertTrue(serial["ok"], serial)
        self.assertEqual(len(serial["results"]), 12)
        self.assertEqual(parallel, serial)

    def test_fanout_hits_carry_vault_and_qualified_path(self):
        out = self._search()
        self.assertTrue(out["results"])
        for row in out["results"]:
            self.assertEqual(row["qualified_path"], f"{row['vault']}:{row['source']}")

    def test_workers_run_concurrently_inside_their_own_bind(self):
        real = ops._search_one_vault
        seen: list[tuple[int, str]] = []
        lock = threading.Lock()
        active = peak = 0

        def probe(query, b, **kw):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return real(query, b, **kw)

        real_search = core.search

        def bound_probe(*a, **kw):
            seen.append((threading.get_ident(), vaults.index_path().name))
            return real_search(*a, **kw)

        with mock.patch.object(ops, "_search_one_vault", probe), mock.patch.object(
            core, "search", bound_probe
        ):
            out = ops.search(
                "parallelzzz", vaults=["alpha", "beta"], folders=["docs", "notes"], limit=20
            )
        self.assertTrue(out["ok"], out)
        self.assertEqual(peak, 3)
        self.assertEqual(
            sorted(index for _tid, index in seen),
            ["alpha.db", "alpha.db", "beta.db", "beta.db"],
        )
        self.assertNotIn(threading.get_ident(), {tid for tid, _i in seen})

    def test_single_pair_stays_on_caller_thread(self):
        real = ops._search_one_vault
        tids: list[int] = []

        def probe(query, b, **kw):
            tids.append(threading.get_ident())
            return real(query, b, **kw)

        with mock.patch.object(ops, "_search_one_vault", probe):
            out = ops.search("parallelzzz", vault="alpha", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(tids, [threading.get_ident()])

    def test_workers_reuse_thread_local_readers_across_fanouts(self):
        binding = self.bindings["alpha"]
        pairs = [(binding, folder) for folder in ("one", "two", "three")]

        def run_once():
            barrier = threading.Barrier(3)

            def connect(b, folder):
                with vaults.bind(b):
                    barrier.wait()
                    core.reader_connect()
                return folder, None

            return ops._run_search_pairs(pairs, connect)

        with mock.patch.object(index_db, "connect", wraps=index_db.connect) as connect:
            self.assertEqual([name for name, _ in run_once()], ["one", "two", "three"])
            self.assertEqual(connect.call_count, 3)
            connect.reset_mock()
            self.assertEqual([name for name, _ in run_once()], ["one", "two", "three"])
            connect.assert_not_called()
            ops._reset_search_fanout_pool()
            self.assertEqual([name for name, _ in run_once()], ["one", "two", "three"])
            self.assertEqual(connect.call_count, 3)

    def test_failure_warnings_follow_pair_order(self):
        real = ops._search_one_vault

        def flaky(query, b, **kw):
            if kw["folder_clean"] == "docs":
                time.sleep(0.05 if b.name == "alpha" else 0.0)  # finish out of order
                raise SystemExit(f"{b.name} down")
            return real(query, b, **kw)

        with mock.patch.object(ops, "_search_one_vault", flaky):
            with mock.patch.object(ops, "_run_search_pairs", _serial_pairs):
                serial = self._search()
            parallel = self._search()
        self.assertTrue(serial["ok"], serial)
        self.assertEqual(
            serial["warning"].count("search_failed"), 2, serial["warning"]
        )
        self.assertEqual(parallel, serial)

    def test_all_pairs_failing_is_an_error(self):
        with mock.patch.object(ops, "_search_one_vault", side_effect=SystemExit("no index")):
            out = self._search()
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"], "search_failed")
        self.assertEqual(out["message"].count("no index"), 4, out)


if __name__ == "__main__":
    unittest.main()
