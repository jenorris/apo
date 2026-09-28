"""search_eval staleness gate + result-composition metrics + check-evals mechanism."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, search_eval

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


class SearchEvalStalenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-staleness-"))
        self.vault = self.tmp / "vault"
        areas = self.vault / "areas"
        areas.mkdir(parents=True)
        (areas / "alive.md").write_text(
            "# Alive\n\nStripe payment processor architecture\n", encoding="utf-8"
        )
        (areas / "other.md").write_text("# Other\n\nunrelated content\n", encoding="utf-8")
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "staleness_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    # --- check_stale_expects -------------------------------------------------- #

    def test_live_path_is_not_stale(self):
        spec = {
            "queries": [
                {"query": "stripe payment", "expect": ["areas/alive.md"], "expect_entity": "Stripe"}
            ]
        }
        report = search_eval.check_stale_expects(spec)
        self.assertTrue(report["resolved"])
        self.assertEqual(report["stale_expect_count"], 0)
        self.assertEqual(report["stale_entity_count"], 0)

    def test_missing_path_is_stale(self):
        spec = {"queries": [{"query": "ghost", "expect": ["areas/does-not-exist.md"]}]}
        report = search_eval.check_stale_expects(spec)
        self.assertTrue(report["resolved"])
        self.assertEqual(report["stale_expect_count"], 1)
        self.assertEqual(report["stale_expect"][0]["expect"], "areas/does-not-exist.md")

    def test_folder_prefix_expect_stale_when_empty_or_missing(self):
        spec = {"queries": [{"query": "ghost folder", "expect": ["projects/nope/"]}]}
        report = search_eval.check_stale_expects(spec)
        self.assertEqual(report["stale_expect_count"], 1)

        spec_ok = {"queries": [{"query": "areas prefix", "expect": ["areas/"]}]}
        report_ok = search_eval.check_stale_expects(spec_ok)
        self.assertEqual(report_ok["stale_expect_count"], 0)

    def test_entity_not_found_flagged_separately_from_stale_expect(self):
        spec = {
            "queries": [
                {
                    "query": "stripe payment",
                    "expect": ["areas/alive.md"],
                    "expect_entity": "TotallyAbsentEntity",
                }
            ]
        }
        report = search_eval.check_stale_expects(spec)
        self.assertEqual(report["stale_expect_count"], 0)
        self.assertEqual(report["stale_entity_count"], 1)

    def test_unresolvable_vault_reports_resolved_false_not_stale(self):
        spec = {"vault": "no-such-vault-in-registry", "queries": [{"query": "x", "expect": ["y.md"]}]}
        report = search_eval.check_stale_expects(spec, vault="no-such-vault-in-registry")
        self.assertFalse(report["resolved"])
        self.assertEqual(report["stale_expect_count"], 0)

    # --- run_eval / format_report integration --------------------------------- #

    def test_run_eval_flags_stale_expect_distinct_from_miss(self):
        eval_file = self.tmp / "eval.yaml"
        eval_file.write_text(
            textwrap.dedent(
                """
                k: 3
                queries:
                  - query: "stripe payment processor"
                    expect: ["areas/alive.md"]
                  - query: "ghost query"
                    expect: ["areas/deleted-note.md"]
                """
            ),
            encoding="utf-8",
        )
        report = search_eval.run_eval(eval_file)
        self.assertEqual(report["stale_expect_count"], 1)
        stale_queries = {r["query"] for r in report["stale_expect"]}
        self.assertIn("ghost query", stale_queries)

        row_by_query = {r["query"]: r for r in report["rows"]}
        self.assertTrue(row_by_query["ghost query"].get("stale_expect_all"))
        self.assertNotIn("stale_expect", row_by_query["stripe payment processor"])

        text = search_eval.format_report(report)
        self.assertIn("STALE FIXTURE", text)
        self.assertIn("STALE expect 'areas/deleted-note.md'", text)

    def test_run_eval_composition_metrics_present(self):
        eval_file = self.tmp / "eval.yaml"
        eval_file.write_text(
            textwrap.dedent(
                """
                k: 2
                queries:
                  - query: "stripe payment processor"
                    expect: ["areas/alive.md"]
                """
            ),
            encoding="utf-8",
        )
        report = search_eval.run_eval(eval_file)
        comp = report["composition"]
        self.assertEqual(comp["k"], 2)
        self.assertEqual(comp["queries_scored"], 1)
        self.assertGreaterEqual(comp["mean_distinct_paths_at_k"], 1.0)
        self.assertGreaterEqual(comp["mean_max_same_path_at_k"], 1.0)
        self.assertIn("kind_share_at_k", comp)
        row = report["rows"][0]
        self.assertIn("distinct_paths_at_k", row)
        self.assertIn("max_same_path_at_k", row)


class CheckEvalsTest(unittest.TestCase):
    """`discover_eval_files` / `check_eval_file` / baseline snapshot mechanics."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-check-evals-"))
        self.vault = self.tmp / "vault"
        (self.vault / "areas").mkdir(parents=True)
        (self.vault / "areas" / "alive.md").write_text(
            "# Alive\n\nStripe payment processor architecture\n", encoding="utf-8"
        )
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "check_evals_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _write_fixture(self, name: str, body: str) -> Path:
        p = self.tmp / name
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return p

    def test_discover_eval_files_skips_examples_and_non_eval_files(self):
        self._write_fixture("search-eval-real.yaml", "k: 3\nqueries: []\n")
        self._write_fixture("search-eval.example.yaml", "k: 3\nqueries: []\n")
        self._write_fixture("search-eval-index.yaml", "# just a readme, no queries here\n")
        found = {p.name for p in search_eval.discover_eval_files([self.tmp])}
        self.assertEqual(found, {"search-eval-real.yaml"})

    def test_check_eval_file_reports_stale_and_can_write_baseline(self):
        fixture = self._write_fixture(
            "search-eval-mini.yaml",
            """
            k: 3
            queries:
              - query: "stripe payment processor"
                expect: ["areas/alive.md"]
              - query: "ghost"
                expect: ["areas/gone.md"]
            """,
        )
        result = search_eval.check_eval_file(fixture)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stale_expect_count"], 1)
        self.assertIsNone(result["baseline"])
        self.assertEqual(search_eval.check_evals_exit_code([result]), 2)

        result_with_baseline = search_eval.check_eval_file(fixture, save_new_baseline=True)
        baseline_path = fixture.parent / "search-eval-mini.baseline.json"
        self.assertTrue(baseline_path.is_file())
        snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["hit_at_k"], result_with_baseline["hit_at_k"])

    def test_check_eval_file_detects_regression_vs_baseline(self):
        fixture = self._write_fixture(
            "search-eval-regress.yaml",
            """
            k: 3
            queries:
              - query: "stripe payment processor"
                expect: ["areas/alive.md"]
            """,
        )
        baseline_path = fixture.parent / "search-eval-regress.baseline.json"
        baseline_path.write_text(
            json.dumps({"hit_at_k": 1.0, "mrr_at_k": 1.0, "k": 3, "queries": 1}), encoding="utf-8"
        )

        # This fixture's one query hits (the vault's single note matches
        # trivially), so hit@k == baseline's 1.0 — zero drop. A regress_threshold
        # below zero still flags that as "regression" without depending on
        # embedding-similarity internals to engineer a real drop.
        result = search_eval.check_eval_file(fixture, regress_threshold=-1.0)
        self.assertFalse(result["stale_expect_count"])
        # Any drop at all now counts as regression (threshold forced below zero),
        # and hit@k for a single real hit is 1.0 == baseline, i.e. drop == 0 > -1.
        self.assertTrue(result["regressed"])
        self.assertEqual(search_eval.check_evals_exit_code([result]), 3)

    def test_check_eval_file_handles_all_queries_erroring(self):
        fixture = self._write_fixture(
            "search-eval-badvault.yaml",
            """
            vault: totally-unregistered-vault
            k: 3
            queries:
              - query: "anything"
                expect: ["areas/alive.md"]
            """,
        )
        result = search_eval.check_eval_file(fixture)
        self.assertFalse(result["ok"])
        self.assertIn("failed", result["error"])
        self.assertEqual(search_eval.check_evals_exit_code([result]), 1)


if __name__ == "__main__":
    unittest.main()
