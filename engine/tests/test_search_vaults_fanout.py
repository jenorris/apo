"""search_notes vaults=[] fan-out across separate indexes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops, vaults

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


class SearchVaultsFanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.alpha = self.tmp / "alpha"
        self.beta = self.tmp / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        (self.alpha / "note-a.md").write_text(
            "---\ntitle: Alpha\n---\n\n# Alpha uniquezzz token\n\nalpha body\n",
            encoding="utf-8",
        )
        (self.beta / "note-b.md").write_text(
            "---\ntitle: Beta\n---\n\n# Beta uniquezzz token\n\nbeta body\n",
            encoding="utf-8",
        )
        self.reg = self.tmp / "vaults.json"
        self.reg.write_text(
            json.dumps(
                {
                    "default": "alpha",
                    "vaults": {
                        "alpha": {
                            "root": str(self.alpha),
                            "index": str(self.tmp / "alpha.db"),
                            "collection": "fanout_alpha",
                        },
                        "beta": {
                            "root": str(self.beta),
                            "index": str(self.tmp / "beta.db"),
                            "collection": "fanout_beta",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        # patch.dict survives conftest's monkeypatch.delenv("APO_VAULTS").
        self._env = mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": str(self.reg),
                "APO_NOTES_ROOT": str(self.alpha),
                "APO_INDEX": str(self.tmp / "legacy.db"),
                "APO_COLLECTION": "legacy",
            },
            clear=False,
        )
        self._env.start()
        self._patches = [
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(config, "SEARCH_EXCLUDE_DEFAULT", []),
            mock.patch.object(config, "NOTES_ROOT", self.alpha),
            mock.patch.object(config, "INDEX_PATH", self.tmp / "legacy.db"),
            mock.patch.object(config, "COLLECTION", "legacy"),
        ]
        for p in self._patches:
            p.start()
        core.clear_query_embed_cache()
        _default, bindings = vaults.load_bindings()
        self.assertEqual(set(bindings), {"alpha", "beta"}, bindings)
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
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fanout_merges_and_stamps_vault(self):
        out = ops.search("uniquezzz", vaults=["alpha", "beta"], limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("vaults"), ["alpha", "beta"])
        self.assertNotIn("vault", out)
        sources = {r["source"] for r in out["results"]}
        vaults_hit = {r["vault"] for r in out["results"]}
        self.assertTrue(any("note-a" in s for s in sources), out)
        self.assertTrue(any("note-b" in s for s in sources), out)
        self.assertEqual(vaults_hit, {"alpha", "beta"})
        scores = [float(r["score"]) for r in out["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_fanout_respects_limit(self):
        out = ops.search("uniquezzz", vaults=["alpha", "beta"], limit=1)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["results"]), 1)

    def test_vault_and_vaults_conflict(self):
        out = ops.search("uniquezzz", vault="alpha", vaults=["beta"], limit=3)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_request")
        self.assertIn("not both", out.get("message", ""))

    def test_unknown_vault_in_vaults(self):
        out = ops.search("uniquezzz", vaults=["alpha", "nope"], limit=3)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_request")
        self.assertIn("nope", out.get("message", ""))

    def test_empty_vaults_bad_request(self):
        out = ops.search("uniquezzz", vaults=["", "  "], limit=3)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "bad_request")
        self.assertIn("non-empty", out.get("message", ""))

    def test_dedupe_vault_names(self):
        out = ops.search("uniquezzz", vaults=["alpha", "alpha", "beta"], limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("vaults"), ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
