"""Tests for hygiene_apply mechanical fixes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apo_engine import hygiene_apply, note_lint


class HygieneApplyTests(unittest.TestCase):
    def test_derive_title_from_h1(self):
        self.assertEqual(
            hygiene_apply.derive_title("# Platform Ops Clinic\n", "areas/x.md"),
            "Platform Ops Clinic",
        )
        self.assertEqual(
            hygiene_apply.derive_title("no heading\n", "areas/my-thread.md"),
            "My thread",
        )

    def test_frontmatter_floor_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "system" / "contracts").mkdir(parents=True)
            (root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
                "frontmatter_floor: [title, status]\n",
                encoding="utf-8",
            )
            content = "# My Thread\n\nBody.\n"
            new, applied = hygiene_apply.apply_frontmatter_floor(
                content, "areas/threads/my-thread.md", vault_root=root
            )
            self.assertIn("title:", new)
            self.assertIn("status: active", new)
            self.assertIn("title", applied)
            self.assertIn("status", applied)

    def test_foreign_wikilink_prefix(self):
        with tempfile.TemporaryDirectory() as tmp_w, tempfile.TemporaryDirectory() as tmp_a:
            work = Path(tmp_w)
            atlas = Path(tmp_a)
            (atlas / "system" / "config").mkdir(parents=True)
            (atlas / "system" / "config" / "source-routing.md").write_text(
                "# Source routing\n", encoding="utf-8"
            )
            content = "See [[system/config/source-routing]].\n"
            vault_roots = {"work": work, "atlas": atlas}
            new, links = hygiene_apply.apply_foreign_wikilink_fixes(
                content,
                path="areas/threads/t.md",
                vault="work",
                vault_root=work,
                vault_roots=vault_roots,
            )
            self.assertIn("[[atlas:system/config/source-routing]]", new)
            self.assertEqual(len(links), 1)

    def test_dialect_wikilink_stub(self):
        content = "---\ntitle: T\nstatus: active\n---\n\nBody only.\n"
        new, applied = hygiene_apply.apply_dialect_wikilink_stub(
            content, "areas/threads/t.md"
        )
        self.assertIn("[[areas/threads/sources]]", new)
        self.assertEqual(applied, ["wikilink_stub"])

    def test_resolve_foreign_prefers_atlas_for_system(self):
        with tempfile.TemporaryDirectory() as tmp_w, tempfile.TemporaryDirectory() as tmp_a:
            work = Path(tmp_w)
            atlas = Path(tmp_a)
            (atlas / "system" / "config").mkdir(parents=True)
            (atlas / "system" / "config" / "foo.md").write_text("# Foo\n", encoding="utf-8")
            match = note_lint.resolve_foreign_wikilink(
                "system/config/foo",
                current_vault="work",
                vault_roots={"work": work, "atlas": atlas},
            )
            self.assertEqual(match, ("atlas", "system/config/foo.md"))

    def test_detect_broken_links_foreign_auto_remediation(self):
        with tempfile.TemporaryDirectory() as tmp_w, tempfile.TemporaryDirectory() as tmp_a:
            work = Path(tmp_w)
            atlas = Path(tmp_a)
            (atlas / "system" / "config").mkdir(parents=True)
            (atlas / "system" / "config" / "graphify.md").write_text("# Graphify\n", encoding="utf-8")
            content = "See [[system/config/graphify]].\n"
            flaws = note_lint.detect_broken_links(
                content,
                path="areas/threads/t.md",
                vault_root=work,
                vault="work",
                vault_roots={"work": work, "atlas": atlas},
            )
            self.assertEqual(len(flaws), 1)
            self.assertEqual(flaws[0].remediation, "auto")
            self.assertEqual(
                flaws[0].evidence.get("suggested_target"),
                "atlas:system/config/graphify",
            )


if __name__ == "__main__":
    unittest.main()
