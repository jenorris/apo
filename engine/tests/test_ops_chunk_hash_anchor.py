"""chunk_hash anchors: path-optional append, patch ops, stale fallback."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops
from apo_engine.chunk_anchor import STALE_HASH_TIP

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


class ChunkHashAnchorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        areas = self.vault / "areas"
        areas.mkdir()
        (areas / "note.md").write_text(
            "---\ntitle: Widget\n---\n\n# Widget\n\nwidget body here\n\n"
            "## Log\n\n- keep\n",
            encoding="utf-8",
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "chunk_hash_anchor_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
            mock.patch.object(ops, "watcher_status", lambda: {"running": True}),
        ]
        for p in self._patches:
            p.start()
        ops._recent_writes.clear()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        ops._recent_writes.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hit_hash(self) -> str:
        hits = core.search("keep", k=1, hybrid=False)
        self.assertTrue(hits)
        return hits[0].chunk_hash

    def test_append_chunk_hash_without_path(self):
        ch = self._hit_hash()
        out = ops.append_note(text="- from hash only\n", chunk_hash=ch)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["path"], "areas/note.md")
        body = (self.vault / "areas" / "note.md").read_text(encoding="utf-8")
        self.assertIn("- from hash only", body)
        self.assertNotIn("stale", out.get("tip", ""))

    def test_append_path_mismatch(self):
        ch = self._hit_hash()
        out = ops.append_note(
            "other.md", text="- nope\n", chunk_hash=ch, create=True
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "path_mismatch")

    def test_append_stale_hash_falls_back_to_heading(self):
        out = ops.append_note(
            "areas/note.md",
            text="- stale ok\n",
            chunk_hash="deadbeef" * 4,
            heading="## Log",
        )
        self.assertTrue(out["ok"], out)
        self.assertIn(STALE_HASH_TIP.split(";")[0].strip(), out.get("tip", ""))
        body = (self.vault / "areas" / "note.md").read_text(encoding="utf-8")
        self.assertIn("- stale ok", body)

    def test_append_stale_hash_without_heading_fails(self):
        out = ops.append_note(
            "areas/note.md",
            text="- nope\n",
            chunk_hash="deadbeef" * 4,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "anchor_not_found")
        self.assertIn("heading=", out["message"])

    def test_patch_append_via_chunk_hash(self):
        ch = self._hit_hash()
        out = ops.patch_note(
            "areas/note.md",
            [{"op": "append", "text": "- patched\n", "chunk_hash": ch}],
        )
        self.assertTrue(out["ok"], out)
        body = (self.vault / "areas" / "note.md").read_text(encoding="utf-8")
        self.assertIn("- patched", body)

    def test_patch_replace_text_via_chunk_hash(self):
        ch = self._hit_hash()
        out = ops.patch_note(
            "areas/note.md",
            [
                {
                    "op": "replace_text",
                    "find": "- keep",
                    "replace": "- kept",
                    "chunk_hash": ch,
                }
            ],
        )
        self.assertTrue(out["ok"], out)
        body = (self.vault / "areas" / "note.md").read_text(encoding="utf-8")
        self.assertIn("- kept", body)
        self.assertNotIn("- keep\n", body)

    def test_patch_stale_chunk_hash_with_heading(self):
        out = ops.patch_note(
            "areas/note.md",
            [
                {
                    "op": "append",
                    "text": "- patch stale\n",
                    "chunk_hash": "cafebabe" * 4,
                    "heading": "## Log",
                }
            ],
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("stale", out.get("tip", ""))
        body = (self.vault / "areas" / "note.md").read_text(encoding="utf-8")
        self.assertIn("- patch stale", body)


if __name__ == "__main__":
    unittest.main()
