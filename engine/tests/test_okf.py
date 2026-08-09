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

    def test_double_star_glob_matches_nested_paths(self):
        """``**`` path_rules must work on Py 3.11/3.12 (no PurePath.full_match)."""
        # Exercise the regex backport even when full_match exists.
        okf._GLOB_RE_CACHE.clear()
        cases = [
            ("areas/threads/foo.md", "areas/threads/**/*.md", True),
            ("areas/threads/nested/foo.md", "areas/threads/**/*.md", True),
            ("areas/other/foo.md", "areas/threads/**/*.md", False),
            ("records/fact.yaml", "records/**/*.yaml", True),
            ("index.md", "index.md", True),
            ("projects/foo/index.md", "index.md", False),
            ("projects/foo/index.md", "**/index.md", True),
            ("inbox/daily/2026-07-17.md", "inbox/daily/*.md", True),
            ("inbox/daily/x/y.md", "inbox/daily/*.md", False),
        ]
        for rel, pat, want in cases:
            self.assertEqual(
                okf._compile_glob(pat).fullmatch(rel) is not None,
                want,
                msg=f"regex {rel!r} vs {pat!r}",
            )
            self.assertEqual(
                okf.path_glob_match(rel, pat),
                want,
                msg=f"path_glob_match {rel!r} vs {pat!r}",
            )

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

    def test_stamps_spec_type_alongside_okf_type(self):
        """SPEC §11 requires a non-empty ``type``; Apo emits it next to okf_type."""
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo thread\n\nbody\n",
        )
        self.assertTrue(r.ok)
        self.assertIn("okf_type", r.stamped)
        self.assertIn("type", r.stamped)
        self.assertIn("okf_type: Thread", r.content)
        self.assertRegex(r.content, r"(?m)^type:\s*\"?Thread\"?$")

    def test_spec_type_fill_preserves_legacy_type_value(self):
        """``fill`` never clobbers a vault's legacy ``type`` taxonomy."""
        content = (
            "---\ntype: project\ntitle: X\ndescription: kept\n"
            "timestamp: 2026-01-01T00:00:00Z\n---\n\n# X\n"
        )
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
        )
        self.assertTrue(r.ok)
        self.assertNotIn("type", r.stamped)
        self.assertRegex(r.content, r"(?m)^type:\s*project$")
        # okf_type still resolves from the path rule
        self.assertEqual(r.okf_type, "Thread")

    def test_spec_type_mirror_overwrites_legacy(self):
        contract = self.root / "system" / "config" / "okf-contract.schema.yaml"
        contract.write_text(
            _MINI_CONTRACT + '\nspec_type_policy: "mirror"\n', encoding="utf-8"
        )
        okf.clear_contract_cache()
        content = (
            "---\ntype: project\ntitle: X\ndescription: kept\n"
            "timestamp: 2026-01-01T00:00:00Z\n---\n\n# X\n"
        )
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
        )
        self.assertTrue(r.ok)
        self.assertIn("type", r.stamped)
        self.assertRegex(r.content, r"(?m)^type:\s*\"?Thread\"?$")

    def test_spec_type_policy_off_env(self):
        os.environ["APO_OKF_SPEC_TYPE"] = "off"
        try:
            r = okf.process_concept(
                vault_root=self.root,
                rel_path="areas/threads/foo.md",
                content="# Foo thread\n\nbody\n",
            )
            self.assertNotIn("type", r.stamped)
            self.assertNotRegex(r.content, r"(?m)^type:")
        finally:
            os.environ.pop("APO_OKF_SPEC_TYPE", None)

    def test_stamped_output_passes_okf_profile_validation(self):
        """Round-trip: what Apo stamps must satisfy SPEC §11."""
        r = okf.process_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo thread\n\nbody\n",
        )
        report = okf.validate_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=r.content,
            profile="okf",
        )
        self.assertTrue(report.ok, report.violations)

    def test_okf_profile_is_weaker_than_apo_profile(self):
        """Missing description/timestamp fails the producer profile, not the spec."""
        content = "---\ntype: Thread\nokf_type: Thread\n---\n\n# Foo\n"
        spec = okf.validate_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
            profile="okf",
        )
        self.assertTrue(spec.ok, spec.violations)

        strict = okf.validate_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
            profile="apo",
        )
        self.assertFalse(strict.ok)
        fields = {v["field"] for v in strict.violations}
        self.assertIn("description", fields)
        self.assertIn("timestamp", fields)

    def test_okf_profile_flags_missing_type(self):
        content = "---\nokf_type: Thread\ndescription: d\ntimestamp: 2026-01-01T00:00:00Z\n---\n\n# Foo\n"
        report = okf.validate_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content=content,
            profile="okf",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0]["field"], "type")

    def test_okf_profile_flags_missing_frontmatter(self):
        report = okf.validate_concept(
            vault_root=self.root,
            rel_path="areas/threads/foo.md",
            content="# Foo\n\nbody\n",
            profile="okf",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0]["field"], "frontmatter")

    def test_okf_profile_flags_reserved_frontmatter(self):
        report = okf.validate_concept(
            vault_root=self.root,
            rel_path="projects/foo/index.md",
            content="---\ntitle: nope\n---\n\n# Index\n",
            profile="okf",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0]["expected"], "absent")

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
    """Exercise the shared ops.write_note path (MCP + RPC façade) with a temp vault + contract."""

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
        ):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profile = self.root / "system" / "config" / "okf-contract.schema.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(_MINI_CONTRACT, encoding="utf-8")

        from apo_engine import config as apo_config
        from apo_engine import ops

        self._cfg = {
            k: getattr(apo_config, k) for k in ("NOTES_ROOT", "INDEX_PATH", "COLLECTION")
        }
        apo_config.NOTES_ROOT = self.root.resolve()
        apo_config.INDEX_PATH = (self.root / "index.db").resolve()
        apo_config.COLLECTION = "okf_test"
        self.ops = ops

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

    def test_write_note_stamps_thread(self):
        out = self.ops.write_note(
            "areas/threads/bar.md",
            "# Bar\n\nhello\n",
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("okf_type"), "Thread")
        self.assertIn("okf_type", out.get("stamped", []))
        written = (self.root / "areas/threads/bar.md").read_text(encoding="utf-8")
        self.assertIn("okf_type: Thread", written)

    def test_write_note_hard_fail_reserved_fm(self):
        out = self.ops.write_note(
            "projects/x/index.md",
            "---\ntitle: bad\n---\n\n# Index\n",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("error"), "okf_validation")
        self.assertFalse((self.root / "projects/x/index.md").exists())


if __name__ == "__main__":
    unittest.main()
