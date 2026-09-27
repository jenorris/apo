"""apo-local (cli_ops.py) search --no-hybrid.

Problem A: ``ops.search`` already accepted ``hybrid=``, but ``apo-local search``
had no flag to reach it (only ``apo-engine search`` did). This pins the new flag
and its default.
"""
from __future__ import annotations

import unittest
from unittest import mock

from apo_engine import cli_ops, ops


class CliOpsNoHybridTest(unittest.TestCase):
    def test_no_hybrid_flag_forwards_hybrid_false(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": "default", "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli_ops.main(["search", "hello", "--no-hybrid"])
        self.assertEqual(rc, 0)
        self.assertFalse(captured.get("hybrid"))

    def test_default_is_hybrid_true(self):
        captured = {}

        def fake_search(query, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "results": [], "vault": "default", "has_more": False}

        with mock.patch.object(ops, "search", side_effect=fake_search):
            rc = cli_ops.main(["search", "hello"])
        self.assertEqual(rc, 0)
        self.assertTrue(captured.get("hybrid"))


if __name__ == "__main__":
    unittest.main()
