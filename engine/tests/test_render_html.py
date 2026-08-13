"""In-memory HTML render core (pandoc) — no adjacent .html writes."""

from __future__ import annotations

import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

import pytest
import yaml

from apo_engine import local_web_contract as lwc
from apo_engine import render_html


def _write_contract(vault: Path, **extra) -> None:
    data = {
        "local_web_version": "0.2",
        "bind": "127.0.0.1",
        "port": 7432,
        "mode": "adaptive",
        "browse_roots": ["areas"],
        "exclude_globs": ["inbox/daily/**"],
        "render": {
            "engine": "pandoc",
            "default_layout": "memo",
            "stylesheet": "system/assets/htmlize.css",
            "template": "system/assets/htmlize.template.html",
            "header": "system/assets/htmlize-header.html",
            "strip_frontmatter": True,
            "resolve_wikilinks": "serve-routes",
        },
    }
    data.update(extra)
    path = vault / lwc.LOCAL_WEB_CONTRACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
class RenderHtmlTests(unittest.TestCase):
    def setUp(self):
        render_html.clear_render_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-render-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "areas").mkdir()
        (self.vault / "areas" / "demo.md").write_text(
            textwrap.dedent(
                """\
                ---
                title: Render Demo
                status: active
                ---
                # Render Demo

                > [!warning] Watch
                > careful

                Hello **world**.
                """
            ),
            encoding="utf-8",
        )
        (self.vault / "inbox" / "daily").mkdir(parents=True)
        (self.vault / "inbox" / "daily" / "secret.md").write_text("# Secret\n", encoding="utf-8")
        _write_contract(self.vault)

    def tearDown(self):
        render_html.clear_render_cache()

    def test_render_ok_no_adjacent_html(self):
        out = render_html.render_note_html(self.vault, "areas/demo.md")
        self.assertTrue(out["ok"])
        self.assertIn("<html", out["html"])
        self.assertIn("Render Demo", out["html"])
        self.assertEqual(out["layout"], "memo")
        self.assertFalse(out.get("cached"))
        self.assertFalse((self.vault / "areas" / "demo.html").exists())
        # No export sidecars either
        self.assertFalse(list(self.vault.rglob("*.export.json")))

    def test_cache_hit_then_invalidate_on_edit(self):
        first = render_html.render_note_html(self.vault, "areas/demo.md")
        second = render_html.render_note_html(self.vault, "areas/demo.md")
        self.assertFalse(first.get("cached"))
        self.assertTrue(second.get("cached"))
        self.assertEqual(first["html"], second["html"])

        note = self.vault / "areas" / "demo.md"
        # Ensure mtime advances even on coarse filesystems
        import time

        time.sleep(0.02)
        note.write_text(note.read_text(encoding="utf-8") + "\nUpdated line.\n", encoding="utf-8")
        third = render_html.render_note_html(self.vault, "areas/demo.md")
        self.assertFalse(third.get("cached"))
        self.assertIn("Updated line", third["html"])
        self.assertNotEqual(third["mtime_ns"], first["mtime_ns"])

    def test_cache_bypass(self):
        render_html.render_note_html(self.vault, "areas/demo.md")
        again = render_html.render_note_html(self.vault, "areas/demo.md", use_cache=False)
        self.assertFalse(again.get("cached"))

    def test_forbidden_excluded(self):
        with self.assertRaises(render_html.RenderError) as cm:
            render_html.render_note_html(self.vault, "inbox/daily/secret.md")
        self.assertEqual(cm.exception.code, "forbidden")

    def test_not_found(self):
        with self.assertRaises(render_html.RenderError) as cm:
            render_html.render_note_html(self.vault, "areas/missing.md")
        self.assertEqual(cm.exception.code, "not_found")

    def test_uses_bundled_assets_when_vault_missing(self):
        # Contract points at vault assets that do not exist → engine bundle.
        out = render_html.render_note_html(self.vault, "areas/demo.md")
        self.assertTrue(out["ok"])
        self.assertIn("body", out["html"])


if __name__ == "__main__":
    unittest.main()
