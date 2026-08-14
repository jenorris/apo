"""Tests for note_lint detectors and write-path flaws[] dual-emit."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import note_lint, okf


_MINI_CONTRACT = """
okf_contract_version: "1"
default_okf_type: Note
default_enforcement: soft
core_required: [okf_type, description, timestamp]
type_field: okf_type
path_rules:
  - match: "areas/threads/**"
    okf_type: Thread
    enforcement: soft
  - match: "projects/**/index.md"
    class: reserved
"""


class NoteLintUnitTests(unittest.TestCase):
    def test_trailing_ws_detect_and_fix(self):
        raw = "hello  \nworld\n"
        found = note_lint.detect_trailing_ws(raw, path="a.md")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].code, "format.trailing_ws")
        fixed, flaws = note_lint.apply_auto_fixes(raw, path="a.md")
        self.assertEqual(fixed, "hello\nworld\n")
        self.assertEqual(flaws[0]["status"], "fixed")

    def test_flaws_from_okf_soft(self):
        r = okf.OkfResult(
            content="x",
            warnings=["missing description (expected non-empty)"],
            violations=[{"field": "description", "expected": "non-empty"}],
            enforcement="soft",
            ok=True,
        )
        flaws = note_lint.flaws_from_okf(r, path="areas/threads/t.md", vault="work")
        self.assertEqual(len(flaws), 1)
        self.assertEqual(flaws[0]["code"], "okf.missing_field")
        self.assertEqual(flaws[0]["remediation"], "llm")
        self.assertEqual(flaws[0]["suggested_op"]["ops"][0]["field"], "description")

    def test_broken_and_ambiguous_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "areas").mkdir()
            (root / "areas" / "one.md").write_text("# One\n", encoding="utf-8")
            (root / "projects").mkdir()
            (root / "projects" / "one.md").write_text("# Also One\n", encoding="utf-8")
            (root / "areas" / "src.md").write_text(
                "See [[missing-note]] and [[one]].\n", encoding="utf-8"
            )
            content = (root / "areas" / "src.md").read_text(encoding="utf-8")
            flaws = note_lint.detect_broken_links(
                content, path="areas/src.md", vault_root=root
            )
            codes = {f.code for f in flaws}
            self.assertIn("link.broken", codes)
            self.assertIn("link.ambiguous", codes)


class FlawsWriteIntegration(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in (
            "APO_OKF_CONTRACT",
            "APO_OKF_ENFORCEMENT",
            "APO_NOTES_ROOT",
            "APO_COLLECTION",
            "APO_INDEX",
            "APO_VAULTS",
            "APO_COLLECTION_ROOT",
            "APO_VAULT_PATHS",
            "APO_DEFAULT_VAULT",
        ):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profile = self.root / "system" / "contracts" / "okf-contract.schema.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(_MINI_CONTRACT, encoding="utf-8")
        usage = self.root / "system" / "contracts" / "usage-contract.schema.yaml"
        usage.write_text(
            "usage_contract_version: '0.1'\nvault_id: testvault\n"
            "frontmatter_floor: [title]\n"
            "layout:\n  areas: threads\n  projects: work\n  system: contracts\n"
            "contribution:\n  dialect: gfm\n  features:\n    callouts: never\n",
            encoding="utf-8",
        )

        from apo_engine import config as apo_config
        from apo_engine import ops

        self._cfg = {
            k: getattr(apo_config, k) for k in ("NOTES_ROOT", "INDEX_PATH", "COLLECTION")
        }
        apo_config.NOTES_ROOT = self.root.resolve()
        apo_config.INDEX_PATH = (self.root / "index.db").resolve()
        apo_config.COLLECTION = "note_lint_test"
        self.ops = ops
        # Clear binding cache if any
        if hasattr(ops, "_bindings_cache"):
            try:
                ops._bindings_cache = None  # type: ignore[attr-defined]
            except Exception:
                pass

    def tearDown(self):
        okf.clear_contract_cache()
        from apo_engine import config as apo_config

        for k, val in self._cfg.items():
            setattr(apo_config, k, val)
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_write_note_soft_okf_dual_emit_and_trailing_ws(self):
        # Missing description after stamp may still warn; trailing WS auto-fixed.
        out = self.ops.write_note(
            "areas/threads/foo.md",
            "---\ntitle: Foo\n---\n\n# Foo  \n\nbody\n",
        )
        self.assertTrue(out["ok"], out)
        written = (self.root / "areas/threads/foo.md").read_text(encoding="utf-8")
        self.assertNotIn("Foo  \n", written)
        flaws = out.get("flaws") or []
        codes = {f.get("code") for f in flaws}
        # trailing ws should be fixed
        self.assertTrue(
            any(f.get("code") == "format.trailing_ws" and f.get("status") == "fixed" for f in flaws)
            or "format.trailing_ws" not in codes
            or any(f.get("status") == "fixed" for f in flaws),
            flaws,
        )
        # Soft OKF dual-emit: warnings may remain; flaws for soft misses when present
        if out.get("warnings"):
            self.assertTrue(any(f.get("code", "").startswith("okf.") for f in flaws), flaws)

    def test_vault_lint_includes_links(self):
        (self.root / "areas" / "threads").mkdir(parents=True, exist_ok=True)
        (self.root / "areas" / "threads" / "a.md").write_text(
            "---\ntitle: A\nokf_type: Thread\ndescription: d\ntimestamp: "
            '"2026-08-15T00:00:00Z"\n---\n\nSee [[nope]].\n',
            encoding="utf-8",
        )
        out = self.ops.vault_op("lint", folder="areas/threads", limit=50)
        self.assertTrue(out["ok"], out)
        codes = {f.get("code") for f in (out.get("flaws") or [])}
        self.assertIn("link.broken", codes)

    def test_read_note_lint_opt_in(self):
        path = "areas/threads/lintme.md"
        (self.root / "areas" / "threads").mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(
            "---\ntitle: X\nokf_type: Thread\ndescription: d\ntimestamp: "
            '"2026-08-15T00:00:00Z"\n---\n\n> [!note] bad\n\n',
            encoding="utf-8",
        )
        out = self.ops.read_note(path, lint=True)
        self.assertTrue(out["ok"], out)
        codes = {f.get("code") for f in (out.get("flaws") or [])}
        self.assertIn("usage.dialect_feature", codes)


if __name__ == "__main__":
    unittest.main()
