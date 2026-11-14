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


class SymlinkedDotTargetTest(unittest.TestCase):
    """A note-named symlink onto .env / .git must not read, write or index the target."""

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="apo-symdot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        v = self.vault = self.tmp / "vault"
        v.mkdir()
        (v / ".env").write_text("SECRET_TOKEN=hunter2\n", encoding="utf-8")
        (v / ".git").mkdir()
        (v / ".git" / "config").write_text("[remote]\nurl=https://u:pat@host\n", encoding="utf-8")
        (v / "leak.md").symlink_to(".env")
        (v / "gitleak.md").symlink_to(".git/config")
        (v / "sub").mkdir()
        (v / "sub" / "real.md").write_text("# real\n", encoding="utf-8")
        (v / "alias.md").symlink_to("sub/real.md")  # an ordinary in-vault symlink still works
        for p in (
            unittest.mock.patch.object(config, "NOTES_ROOT", v),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "symdot"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_read_through_symlink_is_refused(self):
        for rel in ("leak.md", "gitleak.md"):
            out = ops.read_note(path=rel, raw=True)
            self.assertFalse(out.get("ok"), rel)
            self.assertEqual(out.get("error"), "bad_path", rel)
            self.assertNotIn("hunter2", repr(out))
        self.assertTrue(ops.read_note(path="alias.md", raw=True).get("ok"))

    def test_write_through_symlink_is_refused(self):
        out = ops.append_note(path="leak.md", text="more")
        self.assertFalse(out.get("ok"))
        self.assertEqual((self.vault / ".env").read_text(encoding="utf-8"), "SECRET_TOKEN=hunter2\n")

    def test_full_scan_skips_symlinked_dot_targets(self):
        from apo_engine.index_db import _iter_notes

        rels = {p.relative_to(self.vault.resolve()).as_posix() for p in _iter_notes(self.vault, [])}
        self.assertNotIn("leak.md", rels)
        self.assertNotIn("gitleak.md", rels)
        self.assertIn("alias.md", rels)
        self.assertIn("sub/real.md", rels)
