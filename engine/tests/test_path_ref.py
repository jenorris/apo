"""Unit tests for vault_id:rel path peeling (no vault I/O)."""

from __future__ import annotations

import unittest

from apo_engine.path_ref import (
    PathRefError,
    looks_like_vault_prefix,
    merge_vault_arg,
    peel_path_ref,
    qualified_path,
)


class PathRefUnitTests(unittest.TestCase):
    def test_looks_like_prefix(self):
        self.assertTrue(looks_like_vault_prefix("work:areas/x.md"))
        self.assertTrue(looks_like_vault_prefix("atlas:system/config"))
        self.assertFalse(looks_like_vault_prefix("areas/x.md"))
        self.assertFalse(looks_like_vault_prefix("http://example.com/a:b"))
        self.assertFalse(looks_like_vault_prefix("https://example.com"))
        self.assertFalse(looks_like_vault_prefix("/abs/path"))
        self.assertFalse(looks_like_vault_prefix("foo/bar:baz"))  # slash before colon

    def test_peel_known(self):
        known = {"work", "contracts"}
        v, rel = peel_path_ref("work:areas/threads/x.md", known=known)
        self.assertEqual(v, "work")
        self.assertEqual(rel, "areas/threads/x.md")
        v2, rel2 = peel_path_ref("work:/areas/x", known=known)
        self.assertEqual(v2, "work")
        self.assertEqual(rel2, "areas/x")

    def test_peel_unprefixed(self):
        known = {"work"}
        v, rel = peel_path_ref("areas/threads/x.md", known=known)
        self.assertIsNone(v)
        self.assertEqual(rel, "areas/threads/x.md")

    def test_unknown_prefix_bad_vault(self):
        known = {"work", "contracts"}
        with self.assertRaises(PathRefError) as ctx:
            peel_path_ref("atlas:areas/x.md", known=known)
        self.assertEqual(ctx.exception.code, "bad_vault")
        self.assertIn("atlas", ctx.exception.message)

    def test_merge_conflict(self):
        with self.assertRaises(PathRefError) as ctx:
            merge_vault_arg("work", "contracts", default="work")
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_merge_prefix_wins(self):
        self.assertEqual(merge_vault_arg("work", "", default="contracts"), "work")
        self.assertEqual(merge_vault_arg("work", "work", default="contracts"), "work")
        self.assertEqual(merge_vault_arg(None, "contracts", default="work"), "contracts")
        self.assertEqual(merge_vault_arg(None, "", default="work"), "work")

    def test_qualified_path(self):
        self.assertEqual(qualified_path("work", "areas/x.md"), "work:areas/x.md")
        self.assertEqual(qualified_path("work", "/areas/x.md"), "work:areas/x.md")
