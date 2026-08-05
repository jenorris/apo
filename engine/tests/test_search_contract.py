"""search-contract loader + per-vault default excludes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import config, core, ops, search_contract, vaults

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


def _write_search_contract(vault: Path, default_exclude: list[str]) -> None:
    rel = search_contract.SEARCH_CONTRACT_REL
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "search_contract_version": "0.1",
                "default_exclude": default_exclude,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class SearchContractTests(unittest.TestCase):
    def setUp(self):
        search_contract.clear_default_exclude_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-search-contract-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "canonical.md").write_text(
            "# Canon\n\nalpha alpha canonical note\n", encoding="utf-8"
        )
        (self.vault / "noise").mkdir()
        (self.vault / "noise" / "log.md").write_text(
            "# Log\n\nalpha alpha alpha noise entry\n", encoding="utf-8"
        )
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            mock.patch.object(config, "COLLECTION", "search_contract_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", []),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        search_contract.clear_default_exclude_cache()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vault_contract_default_exclude(self):
        _write_search_contract(self.vault, ["noise/*"])
        search_contract.clear_default_exclude_cache()
        out = ops.search("alpha", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("default_exclude"), ["noise/*"])
        sources = [r["source"] for r in out["results"]]
        self.assertNotIn("noise/log.md", sources)

    def test_empty_vault_contract_disables_env_fallback(self):
        _write_search_contract(self.vault, [])
        search_contract.clear_default_exclude_cache()
        with mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", ["noise/*"]):
            out = ops.search("alpha", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("default_exclude", out)
        self.assertIn("noise/log.md", [r["source"] for r in out["results"]])

    def test_env_fallback_without_contract(self):
        with mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", ["noise/*"]):
            out = ops.search("alpha", limit=5)
        self.assertEqual(out.get("default_exclude"), ["noise/*"])
        self.assertNotIn("noise/log.md", [r["source"] for r in out["results"]])

    def test_folder_scoped_skips_default_exclude(self):
        _write_search_contract(self.vault, ["noise/*"])
        search_contract.clear_default_exclude_cache()
        out = ops.search("alpha", folder="noise", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("default_exclude", out)
        self.assertIn("noise/log.md", [r["source"] for r in out["results"]])

    def test_history_browse_inherits_vault_default_exclude(self):
        _write_search_contract(self.vault, ["noise/*"])
        search_contract.clear_default_exclude_cache()
        out = ops.history(limit=10)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("default_exclude"), ["noise/*"])
        paths = {n["path"] for n in out["notes"]}
        self.assertNotIn("noise/log.md", paths)


class SearchContractFanoutTests(unittest.TestCase):
    def setUp(self):
        search_contract.clear_default_exclude_cache()
        self.tmp = Path(tempfile.mkdtemp())
        self.alpha = self.tmp / "alpha"
        self.beta = self.tmp / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        (self.alpha / "note-a.md").write_text(
            "---\ntitle: Alpha\n---\n\n# Alpha uniquezzz token\n\nalpha body\n",
            encoding="utf-8",
        )
        (self.beta / "inbox").mkdir()
        (self.beta / "inbox" / "daily.md").write_text(
            "# Daily\n\nuniquezzz session log\n", encoding="utf-8"
        )
        (self.beta / "note-b.md").write_text(
            "---\ntitle: Beta\n---\n\n# Beta uniquezzz token\n\nbeta body\n",
            encoding="utf-8",
        )
        _write_search_contract(self.alpha, ["inbox/*"])
        _write_search_contract(self.beta, [])
        search_contract.clear_default_exclude_cache()
        self.reg = self.tmp / "vaults.json"
        self.reg.write_text(
            json.dumps(
                {
                    "default": "alpha",
                    "vaults": {
                        "alpha": {
                            "root": str(self.alpha),
                            "index": str(self.tmp / "alpha.db"),
                            "collection": "sc_alpha",
                        },
                        "beta": {
                            "root": str(self.beta),
                            "index": str(self.tmp / "beta.db"),
                            "collection": "sc_beta",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self._env = mock.patch.dict(
            os.environ,
            {"APO_VAULTS": str(self.reg)},
            clear=False,
        )
        self._env.start()
        self._patches = [
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", ["inbox/*"]),
        ]
        for p in self._patches:
            p.start()
        core.clear_query_embed_cache()
        _default, bindings = vaults.load_bindings()
        for name in ("alpha", "beta"):
            with vaults.bind(bindings[name]):
                core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        search_contract.clear_default_exclude_cache()
        core.clear_query_embed_cache()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fanout_per_vault_default_exclude(self):
        out = ops.search("uniquezzz", vaults=["alpha", "beta"], limit=5)
        self.assertTrue(out["ok"], out)
        by_vault = out.get("default_exclude_by_vault") or {}
        self.assertEqual(by_vault.get("alpha"), ["inbox/*"])
        self.assertNotIn("beta", by_vault)
        paths = {r["source"] for r in out["results"]}
        self.assertTrue(any("inbox/daily" in s for s in paths), out)
        self.assertTrue(any("note-a" in s for s in paths), out)


if __name__ == "__main__":
    unittest.main()
