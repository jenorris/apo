"""Search hits carry usage-contract `layout` description for their top-level folder.

Adapted from qmd (github.com/tobi/qmd), which returns descriptive context inline
with each search result so an agent doesn't need the whole vault's folder layout
already in context. Apo already had this data (usage-contract `layout`, used to
render the static desk-projection "Folder layout" section) — this wires the same
dict into search() output per hit.
"""

from __future__ import annotations

import hashlib
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


class SearchFolderContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "changelog").mkdir(parents=True)
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "changelog" / "note.md").write_text(
            "---\ntitle: Note\n---\n\n# Note\n\nsome unique gribblefrotz content\n",
            encoding="utf-8",
        )
        (self.vault / "no-layout.md").write_text(
            "---\ntitle: Root\n---\n\n# Root\n\nsome unique gribblefrotz content\n",
            encoding="utf-8",
        )
        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "layout:\n  changelog: dated log of machine changes\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "folder_context_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        vaults._usage_layout_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hit_under_described_folder_carries_context(self):
        out = ops.search("gribblefrotz", hybrid=False, limit=10)
        rows = {r["source"]: r for r in out["results"]}
        self.assertEqual(
            rows["changelog/note.md"]["folder_context"], "dated log of machine changes"
        )

    def test_hit_outside_any_described_folder_has_no_context_key(self):
        out = ops.search("gribblefrotz", hybrid=False, limit=10)
        rows = {r["source"]: r for r in out["results"]}
        self.assertNotIn("folder_context", rows["no-layout.md"])

    def test_layout_cache_reflects_contract_edits(self):
        out = ops.search("gribblefrotz", hybrid=False, limit=10)
        rows = {r["source"]: r for r in out["results"]}
        self.assertIn("folder_context", rows["changelog/note.md"])

        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "layout: {}\n", encoding="utf-8"
        )
        out2 = ops.search("gribblefrotz", hybrid=False, limit=10)
        rows2 = {r["source"]: r for r in out2["results"]}
        self.assertNotIn("folder_context", rows2["changelog/note.md"])


if __name__ == "__main__":
    unittest.main()
