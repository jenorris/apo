"""OKF write-path stamp / validate tests (no Ollama)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import okf

_MINI_CONTRACT = """
okf_version: "0.1"
type_field: okf_type
legacy_type_field: type
core_required:
  - okf_type
  - description
  - timestamp
core_soft:
  - title
  - resource
default_enforcement: soft
default_okf_type: Note
reserved_filenames:
  - index.md
  - log.md
path_rules:
  - match: "index.md"
    enforcement: exempt
  - match: "**/index.md"
    enforcement: reserved
  - match: "**/log.md"
    enforcement: reserved
  - match: "inbox/daily/*.md"
    enforcement: exempt
    okf_type: Journal
  - match: "projects/pci-2026/R-*/status.md"
    enforcement: hard
    okf_type: EvidenceRequest
    required_fields:
      - okf_type
      - description
      - timestamp
      - title
  - match: "areas/threads/**/*.md"
    enforcement: soft
    okf_type: Thread
legacy_type_map:
  project: Project
  thread: Thread
"""


class OkfStampTests(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profile = self.root / "system" / "config" / "okf-contract.schema.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(_MINI_CONTRACT, encoding="utf-8")

    def tearDown(self):
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_legacy_profile_filename_still_loads(self):
        legacy = self.root / "system" / "config" / "okf-profile.schema.yaml"
        modern = self.root / "system" / "config" / "okf-contract.schema.yaml"
        modern.rename(legacy)
        okf.clear_contract_cache()
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo\n\nbody\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.okf_type, "Thread")

    def test_no_contract_is_off(self):
        empty = Path(tempfile.mkdtemp())
        try:
            r = okf.process_concept(
                vault_root=empty,
                rel_path="areas/threads/foo.md",
                content="# Foo\n\nbody\n",
            )
            self.assertEqual(r.enforcement, "off")
            self.assertTrue(r.ok)
            self.assertEqual(r.content, "# Foo\n\nbody\n")
        finally:
            import shutil

            shutil.rmtree(empty, ignore_errors=True)

    def test_soft_stamps_okf_type_and_description(self):
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo thread\n\nbody\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.enforcement, "soft")
        self.assertEqual(r.okf_type, "Thread")
        self.assertIn("okf_type", r.stamped)
        self.assertIn("description", r.stamped)
        self.assertIn("timestamp", r.stamped)
        self.assertIn("okf_type: Thread", r.content)
        self.assertIn("description:", r.content)
        self.assertTrue(any("derived from H1" in w for w in r.warnings))

    def test_does_not_overwrite_existing_okf_type(self):
        content = "---\nokf_type: Note\ntitle: X\ndescription: kept\ntimestamp: 2026-01-01T00:00:00Z\n---\n\n# X\n"
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.okf_type, "Note")
        self.assertNotIn("okf_type", r.stamped)

    def test_hard_corpus_rejects_wrong_okf_type(self):
        content = (
            "---\n"
            "okf_type: Note\n"
            "title: R-1\n"
            "description: card\n"
            "timestamp: 2026-01-01T00:00:00Z\n"
            "---\n\n# R-1\n"
        )
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="projects/pci-2026/R-0001/status.md",
            content=content,
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.enforcement, "hard")
        self.assertEqual(r.error, "okf_validation")
        self.assertTrue(any(v.get("field") == "okf_type" for v in r.violations))

    def test_hard_corpus_stamps_and_passes(self):
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="projects/pci-2026/R-0001/status.md",
            content="# R-0001 status\n\nbody\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.enforcement, "hard")
        self.assertEqual(r.okf_type, "EvidenceRequest")
        self.assertIn("okf_type: EvidenceRequest", r.content)
        self.assertIn("title:", r.content)

    def test_reserved_index_rejects_frontmatter(self):
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="projects/foo/index.md",
            content="---\ntitle: nope\n---\n\n# Index\n",
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.enforcement, "reserved")
        self.assertEqual(r.error, "okf_validation")

    def test_reserved_index_allows_bare_listing(self):
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="projects/foo/index.md",
            content="# Index\n\n- [[foo]]\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.enforcement, "reserved")
        self.assertEqual(r.content, "# Index\n\n- [[foo]]\n")

    def test_exempt_daily_stamps_timestamp_only(self):
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="inbox/daily/2026-07-17.md",
            content="# 2026-07-17\n\n## Session log\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.enforcement, "exempt")
        self.assertIn("timestamp", r.stamped)
        # exempt does not require okf_type stamp
        self.assertNotIn("okf_type", r.stamped)

    def test_enforcement_off_env(self):
        os.environ["APO_OKF_ENFORCEMENT"] = "off"
        okf.clear_contract_cache()
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo\n",
        )
        self.assertEqual(r.enforcement, "off")
        self.assertEqual(r.content, "# Foo\n")

    def test_resource_from_source_url(self):
        content = (
            "---\n"
            "title: Ingest\n"
            "source_url: https://example.com/a\n"
            "---\n\n# Ingest\n"
        )
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="resources/wiki/example/a.md",
            content=content,
        )
        self.assertTrue(r.ok)
        self.assertIn("resource", r.stamped)
        self.assertRegex(r.content, r'resource:\s*"?https://example\.com/a"?')

    def test_as_response_fields(self):
        r = okf.OkfResult(
            content="x",
            stamped=["okf_type"],
            warnings=["w"],
            okf_type="Thread",
            enforcement="soft",
        )
        fields = r.as_response_fields()
        self.assertEqual(fields["enforcement"], "soft")
        self.assertEqual(fields["stamped"], ["okf_type"])
        self.assertEqual(fields["okf_type"], "Thread")


class OkfWriteNoteIntegration(unittest.TestCase):
    """Exercise MCP write_note sync path with a temp vault + contract."""

    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT", "APO_NOTES_ROOT", "APO_COLLECTION", "APO_INDEX"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profile = self.root / "system" / "config" / "okf-contract.schema.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(_MINI_CONTRACT, encoding="utf-8")
        os.environ["APO_NOTES_ROOT"] = str(self.root)
        os.environ["APO_COLLECTION"] = "okf_test"
        os.environ["APO_INDEX"] = str(self.root / "index.db")

        import importlib.util
        import sys

        engine = Path(__file__).resolve().parents[1]
        server_path = engine / "mcp" / "server.py"
        # Ensure apo_engine is importable from src/
        src = str(engine / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        # Refresh config paths picked up at import
        from apo_engine import config as apo_config

        apo_config.NOTES_ROOT = self.root.resolve()
        apo_config.INDEX_PATH = (self.root / "index.db").resolve()
        apo_config.COLLECTION = "okf_test"

        spec = importlib.util.spec_from_file_location("apo_mcp_okf_test", server_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.VAULTS.clear()
        mod._load_vaults()
        self.server = mod

    def tearDown(self):
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        try:
            self.server.VAULTS.clear()
        except Exception:
            pass

    def test_write_note_stamps_thread(self):
        out = self.server._write_note_sync(
            "areas/threads/bar.md",
            "# Bar\n\nhello\n",
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("okf_type"), "Thread")
        self.assertIn("okf_type", out.get("stamped", []))
        written = (self.root / "areas/threads/bar.md").read_text(encoding="utf-8")
        self.assertIn("okf_type: Thread", written)

    def test_write_note_hard_fail_reserved_fm(self):
        out = self.server._write_note_sync(
            "projects/x/index.md",
            "---\ntitle: bad\n---\n\n# Index\n",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("error"), "okf_validation")
        self.assertFalse((self.root / "projects/x/index.md").exists())


if __name__ == "__main__":
    unittest.main()
