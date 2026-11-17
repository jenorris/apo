"""Oversized JSON catalogs must not become one multi-megabyte chunk."""

from __future__ import annotations

import json
import unittest

from apo_engine.note_format import chunk_json_note, json_index_documents


class JsonIndexCapTests(unittest.TestCase):
    def test_small_object_is_stored_whole(self):
        data = {"title": "widget", "n": 1}
        text, fm = json_index_documents(data, max_chars=1000)
        self.assertEqual(json.loads(text), {"n": 1, "title": "widget"})
        self.assertEqual(json.loads(fm), data)

    def test_large_object_keeps_scalars_and_drops_arrays(self):
        data = {"title": "iam", "rows": [{"user": "a"}] * 4000, "note": "x" * 800}
        text, fm = json_index_documents(data, max_chars=2000)
        self.assertLessEqual(len(text), 2000)
        self.assertNotIn('"user"', text)
        parsed = json.loads(fm)
        self.assertEqual(parsed["title"], "iam")
        self.assertEqual(parsed["rows"], {"_omitted": "array", "len": 4000})
        self.assertTrue(parsed["note"].endswith("…"))
        self.assertLessEqual(len(parsed["note"]), 501)

    def test_chunk_json_note_uses_the_cap(self):
        data = {"title": "export", "blob": "z" * 80_000}
        rows = chunk_json_note("{}\n", "exports/raw.json", data)
        self.assertEqual(len(rows), 1)
        self.assertLess(len(rows[0][2]), 80_000)
        self.assertIn("export", rows[0][2])


if __name__ == "__main__":
    unittest.main()
