"""Phase 2: host copy, vault scope, note reads, symlink replace, git ref."""

from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import config, git_sync, ops
from apo_engine.atomic_io import atomic_write_text
from apo_engine.vault_scope import VaultScopeMiddleware
from fastmcp.exceptions import ToolError


class AllowRootsTest(unittest.TestCase):
    def test_unset_allow_roots_are_empty(self):
        with unittest.mock.patch.object(config, "SEND_ALLOW_ROOTS", ""):
            self.assertEqual(ops._send_allow_roots(), [])


class PlaceScopeTest(unittest.TestCase):
    def test_scoped_place_of_home_file_is_out_of_scope(self):
        mw = VaultScopeMiddleware(lambda: ["public"])
        with self.assertRaises(ToolError) as ctx:
            mw._referenced(
                {
                    "vault": "public",
                    "ops": [{"op": "place", "src": "~/secret.md", "dst": "inbox/x.md"}],
                }
            )
        self.assertIn("vault_out_of_scope", str(ctx.exception))


class ReadNoteGateTest(unittest.TestCase):
    def test_dot_segment_is_not_a_note(self):
        from apo_engine.note_format import is_exposed_note_rel

        self.assertFalse(is_exposed_note_rel(".git/config"))
        self.assertFalse(is_exposed_note_rel(".env"))
        self.assertFalse(is_exposed_note_rel(".apo/watch-hooks.json"))
        self.assertTrue(is_exposed_note_rel("areas/note.md"))


class SymlinkWriteTest(unittest.TestCase):
    def test_atomic_write_refuses_symlink(self):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="apo-sym-"))
        try:
            target = tmp / "target.txt"
            target.write_text("secret", encoding="utf-8")
            link = tmp / "link.md"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                atomic_write_text(link, "replaced")
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "secret")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class GitRefTest(unittest.TestCase):
    def test_option_like_branch_becomes_main(self):
        self.assertEqual(git_sync._validated_branch("--upload-pack=evil"), "main")
        self.assertEqual(git_sync._validated_branch("main"), "main")
        self.assertEqual(git_sync._validated_remote("-origin"), "")
        self.assertEqual(git_sync._validated_remote("origin"), "origin")
