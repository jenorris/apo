"""local-web-contract loader + browse/exclude path gates."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import local_web_contract as lwc


def _write_contract(vault: Path, data: dict) -> Path:
    rel = lwc.LOCAL_WEB_CONTRACT_REL
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class LocalWebContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-local-web-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "areas").mkdir()
        (self.vault / "projects").mkdir()
        (self.vault / "areas" / "note.md").write_text("# Note\n\nhello\n", encoding="utf-8")
        (self.vault / "inbox").mkdir()
        (self.vault / "inbox" / "daily").mkdir(parents=True)
        (self.vault / "inbox" / "daily" / "log.md").write_text("# Log\n", encoding="utf-8")

    def tearDown(self):
        lwc.clear_local_web_cache()

    def test_load_missing(self):
        self.assertIsNone(lwc.load_local_web_contract(self.vault))

    def test_load_and_defaults(self):
        _write_contract(
            self.vault,
            {
                "local_web_version": "0.2",
                "bind": "127.0.0.1",
                "port": 7432,
                "mode": "adaptive",
                "browse_roots": [],
                "exclude_globs": ["inbox/daily/**"],
            },
        )
        data = lwc.load_local_web_contract(self.vault)
        self.assertIsNotNone(data)
        roots = lwc.resolve_browse_roots(self.vault, data)
        self.assertIn("areas", roots)
        self.assertIn("projects", roots)
        bind, port = lwc.resolve_bind_port(data)
        self.assertEqual(bind, "127.0.0.1")
        self.assertEqual(port, 7432)

    def test_explicit_browse_roots_win(self):
        _write_contract(
            self.vault,
            {"browse_roots": ["areas"], "mode": "para"},
        )
        data = lwc.load_local_web_contract(self.vault)
        self.assertEqual(lwc.resolve_browse_roots(self.vault, data), ["areas"])

    def test_force_public_bind_clamped(self):
        bind, _port = lwc.resolve_bind_port({"bind": "0.0.0.0", "port": 9})
        self.assertEqual(bind, "127.0.0.1")

    def test_exclude_globs(self):
        _write_contract(
            self.vault,
            {
                "browse_roots": [],
                "exclude_globs": ["inbox/daily/**", "system/audit/**"],
            },
        )
        data = lwc.load_local_web_contract(self.vault)
        ok, reason = lwc.is_path_allowed(self.vault, "inbox/daily/log.md", data=data)
        self.assertFalse(ok)
        self.assertIn("exclude", reason)
        ok2, _ = lwc.is_path_allowed(self.vault, "areas/note.md", data=data)
        self.assertTrue(ok2)

    def test_outside_browse_roots(self):
        _write_contract(self.vault, {"browse_roots": ["areas"], "exclude_globs": []})
        data = lwc.load_local_web_contract(self.vault)
        ok, reason = lwc.is_path_allowed(self.vault, "projects/x.md", data=data)
        self.assertFalse(ok)
        self.assertIn("browse_roots", reason)

    def test_env_override(self):
        alt = self.tmp / "alt.yaml"
        alt.write_text(
            yaml.safe_dump({"browse_roots": ["projects"], "exclude_globs": []}),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {"APO_LOCAL_WEB_CONTRACT": str(alt)}):
            data = lwc.load_local_web_contract(self.vault)
        self.assertEqual(lwc.resolve_browse_roots(self.vault, data), ["projects"])

    def test_llm_wiki_roots(self):
        wiki_vault = self.tmp / "wiki-vault"
        wiki_vault.mkdir()
        (wiki_vault / "wiki").mkdir()
        (wiki_vault / "raw").mkdir()
        roots = lwc.derive_browse_roots(wiki_vault, "adaptive")
        self.assertEqual(roots, ["wiki", "raw"])

    def test_path_matches_globs_prefix(self):
        self.assertTrue(lwc.path_matches_globs("inbox/daily/x.md", ["inbox/daily/**"]))
        self.assertFalse(lwc.path_matches_globs("areas/x.md", ["inbox/daily/**"]))


class OfmUnwrapTests(unittest.TestCase):
    def test_callout_and_wikilink_routes(self):
        from apo_engine import ofm_unwrap

        raw = textwrap.dedent(
            """\
            ---
            title: Demo
            htmlize_layout: memo
            ---
            # Demo

            > [!tip] Remember
            > body

            See [[areas/note|Note]] and ==highlight==.
            """
        )
        meta, layout, body = ofm_unwrap.build_gfm_body(
            raw,
            Path("areas/demo.md"),
            resolve_wikilinks="serve-routes",
            path_allowed=lambda _p: True,
        )
        self.assertEqual(meta["title"], "Demo")
        self.assertEqual(layout, "memo")
        self.assertIn('class="ofm ofm-tip"', body)
        self.assertIn("/note?path=", body)
        self.assertIn("**highlight**", body)


if __name__ == "__main__":
    unittest.main()
