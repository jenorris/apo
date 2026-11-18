"""Region-aware write preconditions (FM/body/section vs whole-file mtime)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, ops
from apo_engine.write_guard import file_region_hashes

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


_NOTE = """---
title: Widget
status: active
---

# Widget

## History

- old

## Next

- todo
"""


class WriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        areas = self.vault / "areas"
        areas.mkdir()
        self.path = "areas/note.md"
        (areas / "note.md").write_text(_NOTE, encoding="utf-8")
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "write_guard_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
            mock.patch.object(ops, "watcher_status", lambda: {"running": True}),
        ]
        for p in self._patches:
            p.start()
        ops._recent_touches.clear()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        ops._recent_touches.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _full(self) -> Path:
        return self.vault / self.path

    def _bump_mtime_only(self) -> None:
        """Advance mtime without changing bytes (rare); prefer content edits in tests."""
        full = self._full()
        now = time.time() + 5
        os.utime(full, (now, now))

    def test_read_returns_region_hashes(self):
        out = ops.read_note(self.path)
        self.assertTrue(out["ok"], out)
        self.assertIn("frontmatter_hash", out)
        self.assertIn("body_hash", out)
        self.assertEqual(len(out["frontmatter_hash"]), 16)
        regions = file_region_hashes(self._full().read_text(encoding="utf-8"))
        self.assertEqual(out["frontmatter_hash"], regions.frontmatter_hash)
        self.assertEqual(out["body_hash"], regions.body_hash)

    def test_stale_mtime_rejects_without_region_match(self):
        read = ops.read_note(self.path)
        mtime = read["mtime"]
        # Unrelated body edit
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(text.replace("- old", "- old\n- other"), encoding="utf-8")
        ops._recent_touches.clear()  # no process-local snapshot

        bad = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "waiting"}],
            expected_mtime=mtime,
        )
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["error"], "stale_write")

    def test_fm_patch_allowed_when_only_body_changed_via_cache(self):
        read = ops.read_note(self.path)
        mtime = read["mtime"]
        # Body-only edit after the read (cache still holds prior FM/body hashes + mtime)
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(text.replace("- old", "- old\n- other"), encoding="utf-8")

        ok = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "waiting"}],
            expected_mtime=mtime,
        )
        self.assertTrue(ok["ok"], ok)
        self.assertIn("waiting", self._full().read_text(encoding="utf-8"))

    def test_fm_patch_allowed_with_explicit_frontmatter_hash(self):
        read = ops.read_note(self.path)
        ops._recent_touches.clear()
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(text.replace("- old", "- old\n- other"), encoding="utf-8")

        ok = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "snoozed"}],
            expected_mtime=read["mtime"],
            expected_frontmatter_hash=read["frontmatter_hash"],
        )
        self.assertTrue(ok["ok"], ok)

    def test_section_append_allowed_when_only_fm_changed(self):
        read = ops.read_note(self.path)
        mtime = read["mtime"]
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(
            text.replace("status: active", "status: waiting"), encoding="utf-8"
        )

        ok = ops.append_note(
            self.path,
            "- new history\n",
            heading="## History",
            expected_mtime=mtime,
        )
        self.assertTrue(ok["ok"], ok)
        self.assertIn("- new history", self._full().read_text(encoding="utf-8"))

    def test_section_append_rejects_when_same_section_changed(self):
        read = ops.read_note(self.path)
        mtime = read["mtime"]
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(text.replace("- old", "- mutated"), encoding="utf-8")

        bad = ops.append_note(
            self.path,
            "- new history\n",
            heading="## History",
            expected_mtime=mtime,
        )
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["error"], "stale_write")
        self.assertEqual(bad.get("stale_region"), "section")

    def test_explicit_content_hash_allows_section_write(self):
        read = ops.read_note(self.path, heading="## History")
        self.assertIn("content_hash", read)
        ops._recent_touches.clear()
        # Change Next section only
        text = self._full().read_text(encoding="utf-8")
        self._full().write_text(text.replace("- todo", "- done"), encoding="utf-8")

        ok = ops.append_note(
            self.path,
            "- kept\n",
            heading="## History",
            expected_mtime=read["mtime"],
            expected_content_hash=read["content_hash"],
        )
        self.assertTrue(ok["ok"], ok)

    def test_matching_mtime_still_fast_path(self):
        read = ops.read_note(self.path)
        ok = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "active"}],
            expected_mtime=read["mtime"],
        )
        self.assertTrue(ok["ok"], ok)

    def test_search_hits_include_content_hash(self):
        out = ops.search("history", limit=3, folder="areas")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["results"])
        self.assertIn("content_hash", out["results"][0])
        self.assertEqual(len(out["results"][0]["content_hash"]), 16)

    def test_ms_rounded_expected_mtime_uses_region_hash(self):
        full = self._full()
        # Sub-millisecond digits so rounding to ms really moves the value.
        os.utime(full, ns=(1794236165_123_456_789, 1794236165_123_456_789))
        read = ops.read_note(self.path)
        rounded = round(read["mtime"], 3)
        self.assertGreater(abs(rounded - read["mtime"]), 1e-6)

        # Unchanged file: the rounded value misses the exact fast path but
        # matches the cached touch, and the region hashes agree.
        ok = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "waiting"}],
            expected_mtime=rounded,
        )
        self.assertTrue(ok["ok"], ok)

        # Edited in between (same millisecond): hashes still reject it.
        read2 = ops.read_note(self.path)
        ns = full.stat().st_mtime_ns
        text = full.read_text(encoding="utf-8")
        full.write_text(text.replace("status: waiting", "status: external"), encoding="utf-8")
        os.utime(full, ns=(ns + 400_000, ns + 400_000))

        bad = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "mine"}],
            expected_mtime=round(read2["mtime"], 3),
        )
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["error"], "stale_write")
        self.assertEqual(bad["stale_region"], "frontmatter")
        self.assertIn("status: external", full.read_text(encoding="utf-8"))

    def test_stale_write_payload_carries_current_hashes_and_retry_tip(self):
        read = ops.read_note(self.path)
        full = self._full()
        text = full.read_text(encoding="utf-8")
        full.write_text(text.replace("status: active", "status: external"), encoding="utf-8")
        self._bump_mtime_only()

        bad = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "mine"}],
            expected_mtime=read["mtime"],
        )
        self.assertEqual(bad["error"], "stale_write", bad)
        regions = file_region_hashes(full.read_text(encoding="utf-8"))
        self.assertEqual(bad["frontmatter_hash"], regions.frontmatter_hash)
        self.assertEqual(bad["body_hash"], regions.body_hash)
        self.assertEqual(bad["content_hash"], regions.body_hash)
        self.assertEqual(bad["actual_mtime"], full.stat().st_mtime)
        self.assertIn(f"expected_mtime={bad['actual_mtime']!r}", bad["tip"])
        self.assertIn("re-read", bad["tip"])

        # The tip is sufficient: retrying with actual_mtime goes through.
        retry = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "mine"}],
            expected_mtime=bad["actual_mtime"],
        )
        self.assertTrue(retry["ok"], retry)

    def test_integer_truncated_expected_mtime_uses_region_hash(self):
        base_sec = 1794236165
        full = self._full()
        os.utime(full, (base_sec + 0.91, base_sec + 0.91))
        read = ops.read_note(self.path)
        self.assertEqual(int(read["mtime"]), base_sec)

        # Unchanged file + integer-truncated expected_mtime should succeed via region hashes.
        ok = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "waiting"}],
            expected_mtime=float(base_sec),
        )
        self.assertTrue(ok["ok"], ok)

        # Same-second external FM edit must still reject with stale_write!
        os.utime(full, (base_sec + 0.95, base_sec + 0.95))
        read2 = ops.read_note(self.path)
        text = full.read_text(encoding="utf-8")
        full.write_text(text.replace("status: waiting", "status: external"), encoding="utf-8")
        os.utime(full, (base_sec + 0.99, base_sec + 0.99))

        bad = ops.patch_note(
            self.path,
            [{"op": "set_field", "field": "status", "value": "mine"}],
            expected_mtime=float(int(read2["mtime"])),
        )
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["error"], "stale_write")
        self.assertEqual(bad["stale_region"], "frontmatter")


if __name__ == "__main__":
    unittest.main()
