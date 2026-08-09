"""`apo-engine okf` — validate | fix | init | export | ingest, plus read-only vaults."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import okf, okf_cli

CONTRACT = """
okf_version: "0.1"
type_field: okf_type
legacy_type_field: type
spec_type_field: type
spec_type_policy: fill
core_required:
  - okf_type
  - description
  - timestamp
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
  - match: "areas/threads/**/*.md"
    enforcement: soft
    okf_type: Thread
"""

NATIVE_ONLY = '---\nokf_type: Thread\ndescription: d\ntimestamp: "2026-01-01T00:00:00Z"\n---\n\n# Native\n'
CONFORMANT = (
    '---\ntype: Thread\nokf_type: Thread\ndescription: d\n'
    'timestamp: "2026-01-01T00:00:00Z"\n---\n\n# Good\n'
)


class OkfCliBase(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT", "APO_OKF_SPEC_TYPE", "APO_VAULTS"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "okf-contract.schema.yaml").write_text(
            CONTRACT, encoding="utf-8"
        )
        (self.root / "areas" / "threads").mkdir(parents=True)
        self.write("areas/threads/native.md", NATIVE_ONLY)
        self.write("areas/threads/good.md", CONFORMANT)
        self.write("index.md", '---\nokf_version: "0.1"\n---\n\n# Root\n')

    def tearDown(self):
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class ValidateTests(OkfCliBase):
    def test_okf_profile_flags_note_missing_spec_type(self):
        summary = okf_cli.validate_vault(self.root, profile="okf")
        self.assertFalse(summary.ok)
        paths = {v["path"] for v in summary.violations}
        self.assertIn("areas/threads/native.md", paths)
        self.assertNotIn("areas/threads/good.md", paths)

    def test_apo_profile_passes_what_okf_profile_rejects(self):
        """The historical lint was weaker than the spec — that gap is the point."""
        self.assertTrue(okf_cli.validate_vault(self.root, profile="apo").ok)
        self.assertFalse(okf_cli.validate_vault(self.root, profile="okf").ok)

    def test_bundle_root_index_is_not_asked_for_a_type(self):
        """SPEC §11.3 governs reserved filenames; §11.2 does not apply to them."""
        summary = okf_cli.validate_vault(self.root, profile="okf")
        self.assertNotIn("index.md", {v["path"] for v in summary.violations})

    def test_non_root_index_with_frontmatter_is_flagged(self):
        self.write("areas/index.md", "---\ntitle: nope\n---\n\n# Index\n")
        summary = okf_cli.validate_vault(self.root, profile="okf")
        offenders = {v["path"]: v for v in summary.violations}
        self.assertIn("areas/index.md", offenders)
        self.assertEqual(offenders["areas/index.md"]["expected"], "absent")

    def test_paths_scope_limits_the_walk(self):
        summary = okf_cli.validate_vault(
            self.root, profile="okf", paths=[self.root / "areas/threads/good.md"]
        )
        self.assertEqual(summary.scanned, 1)
        self.assertTrue(summary.ok)

    def test_summary_json_shape(self):
        payload = okf_cli.validate_vault(self.root, profile="okf").as_dict()
        self.assertEqual(
            set(payload), {"ok", "profile", "scanned", "violations", "warnings"}
        )
        json.dumps(payload)  # must be serializable


class FixTests(OkfCliBase):
    def test_fix_fills_spec_type_and_validates_clean(self):
        out = okf_cli.fix_vault(self.root)
        self.assertTrue(out["ok"], out)
        fixed = {e["path"] for e in out["fixed"]}
        self.assertIn("areas/threads/native.md", fixed)
        self.assertIn("type: Thread", self.read("areas/threads/native.md"))
        self.assertTrue(okf_cli.validate_vault(self.root, profile="okf").ok)

    def test_dry_run_writes_nothing(self):
        before = self.read("areas/threads/native.md")
        out = okf_cli.fix_vault(self.root, dry_run=True)
        self.assertTrue(out["fixed"])
        self.assertEqual(self.read("areas/threads/native.md"), before)

    def test_fix_is_idempotent(self):
        okf_cli.fix_vault(self.root)
        second = okf_cli.fix_vault(self.root)
        self.assertEqual(second["fixed"], [])

    def test_fix_preserves_legacy_type_value(self):
        self.write(
            "areas/threads/legacy.md",
            '---\ntype: project\nokf_type: Project\ndescription: d\n'
            'timestamp: "2026-01-01T00:00:00Z"\n---\n\n# L\n',
        )
        okf_cli.fix_vault(self.root)
        self.assertIn("type: project", self.read("areas/threads/legacy.md"))


class InitTests(OkfCliBase):
    def test_init_scaffolds_a_validatable_bundle(self):
        fresh = Path(self.tmp.name) / "fresh"
        fresh.mkdir()
        out = okf_cli.init_bundle(fresh)
        self.assertTrue(out["ok"])
        self.assertIn("index.md", out["created"])
        contract = fresh / "system" / "contracts" / "okf-contract.schema.yaml"
        self.assertTrue(contract.is_file())
        # The scaffold must load as a real contract, not just be a text blob.
        loaded = okf.load_contract(contract)
        self.assertEqual(loaded.spec_type_field, "type")
        self.assertEqual(loaded.spec_type_policy, "fill")

    def test_init_does_not_clobber_without_force(self):
        out = okf_cli.init_bundle(self.root)
        self.assertIn("index.md", out["skipped"])
        self.assertIn('okf_version: "0.1"', self.read("index.md"))

    def test_init_force_overwrites(self):
        out = okf_cli.init_bundle(self.root, force=True)
        self.assertIn("index.md", out["created"])

    def test_init_rejects_missing_directory(self):
        out = okf_cli.init_bundle(Path(self.tmp.name) / "nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "not_a_directory")


class ExportTests(OkfCliBase):
    def test_export_makes_a_conformant_bundle(self):
        dest = Path(self.tmp.name) / "export"
        out = okf_cli.export_bundle(self.root, dest, roots=["areas"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["stamped_type"], 1)
        self.assertIn("type: Thread", (dest / "areas/threads/native.md").read_text())
        # Source untouched — export must not mutate the vault.
        self.assertNotRegex(self.read("areas/threads/native.md"), r"(?m)^type:")
        self.assertTrue(okf_cli.validate_vault(dest, profile="okf").ok)

    def test_export_ships_the_contract(self):
        dest = Path(self.tmp.name) / "export2"
        okf_cli.export_bundle(self.root, dest, roots=["areas"])
        self.assertTrue((dest / "system/contracts/okf-contract.schema.yaml").is_file())

    def test_export_declares_requested_version(self):
        dest = Path(self.tmp.name) / "export3"
        okf_cli.export_bundle(self.root, dest, roots=["areas"], okf_version="0.2")
        self.assertIn('okf_version: "0.2"', (dest / "index.md").read_text())

    def test_export_skips_notes_without_any_type(self):
        self.write("areas/threads/untyped.md", "---\ntitle: x\n---\n\n# X\n")
        dest = Path(self.tmp.name) / "export4"
        out = okf_cli.export_bundle(self.root, dest, roots=["areas"])
        self.assertEqual(out["skipped"], 1)
        self.assertFalse((dest / "areas/threads/untyped.md").exists())

    def test_archive_round_trip(self):
        import tarfile

        dest = Path(self.tmp.name) / "staging"
        okf_cli.export_bundle(self.root, dest, roots=["areas"])
        archive = Path(self.tmp.name) / "bundle.tar.gz"
        okf_cli.archive_bundle(dest, archive)
        self.assertTrue(archive.is_file())
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertTrue(any(n.endswith("areas/threads/good.md") for n in names), names)


class IngestTests(OkfCliBase):
    def _conformant_bundle(self) -> Path:
        dest = Path(self.tmp.name) / "foreign"
        okf_cli.export_bundle(self.root, dest, roots=["areas"])
        return dest

    def test_ingest_registers_a_read_only_vault(self):
        vaults_file = Path(self.tmp.name) / "vaults.json"
        out = okf_cli.ingest_bundle(
            self._conformant_bundle(), "foreign", vaults_file=vaults_file
        )
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["read_only"])
        data = json.loads(vaults_file.read_text())
        self.assertTrue(data["vaults"]["foreign"]["read_only"])

    def test_ingest_refuses_a_non_conformant_bundle(self):
        vaults_file = Path(self.tmp.name) / "vaults.json"
        out = okf_cli.ingest_bundle(self.root, "bad", vaults_file=vaults_file)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "not_conformant")
        self.assertFalse(vaults_file.exists())

    def test_ingest_force_overrides_the_gate(self):
        vaults_file = Path(self.tmp.name) / "vaults.json"
        out = okf_cli.ingest_bundle(self.root, "bad", vaults_file=vaults_file, force=True)
        self.assertTrue(out["ok"])
        self.assertFalse(out["conformant"])

    def test_ingest_will_not_replace_an_entry_without_force(self):
        vaults_file = Path(self.tmp.name) / "vaults.json"
        bundle = self._conformant_bundle()
        okf_cli.ingest_bundle(bundle, "foreign", vaults_file=vaults_file)
        out = okf_cli.ingest_bundle(bundle, "foreign", vaults_file=vaults_file)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "vault_exists")

    def test_ingest_preserves_existing_entries(self):
        vaults_file = Path(self.tmp.name) / "vaults.json"
        vaults_file.write_text(
            json.dumps({"default": "meta", "vaults": {"meta": {"root": "/tmp/meta"}}}),
            encoding="utf-8",
        )
        okf_cli.ingest_bundle(self._conformant_bundle(), "foreign", vaults_file=vaults_file)
        data = json.loads(vaults_file.read_text())
        self.assertEqual(set(data["vaults"]), {"meta", "foreign"})
        self.assertEqual(data["default"], "meta")

    def test_ingest_rejects_missing_source(self):
        out = okf_cli.ingest_bundle(
            Path(self.tmp.name) / "nope", "x", vaults_file=Path(self.tmp.name) / "v.json"
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "not_a_directory")


class ReadOnlyVaultTests(OkfCliBase):
    """A read-only vault is searchable but rejects every write op."""

    def setUp(self):
        super().setUp()
        from apo_engine import ops, vaults

        self.ops = ops
        self.vaults_file = Path(self.tmp.name) / "vaults.json"
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "ro",
                    "vaults": {
                        "ro": {
                            "root": str(self.root),
                            "index": str(Path(self.tmp.name) / "ro.db"),
                            "read_only": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(self.vaults_file)
        _ = vaults

    def test_binding_carries_read_only(self):
        from apo_engine import vaults

        _, bindings = vaults.load_bindings()
        self.assertTrue(bindings["ro"].read_only)
        self.assertTrue(bindings["ro"].resolved().read_only)

    def test_write_note_rejected(self):
        out = self.ops.write_note("areas/threads/new.md", "# new\n", vault="ro")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "read_only_vault")
        self.assertFalse((self.root / "areas/threads/new.md").exists())

    def test_append_note_rejected(self):
        out = self.ops.append_note("areas/threads/good.md", text="more", vault="ro")
        self.assertEqual(out["error"], "read_only_vault")

    def test_patch_note_rejected(self):
        out = self.ops.patch_note(
            "areas/threads/good.md",
            [{"op": "set_field", "field": "status", "value": "x"}],
            vault="ro",
        )
        self.assertEqual(out["error"], "read_only_vault")

    def test_delete_note_rejected(self):
        out = self.ops.delete_note("areas/threads/good.md", vault="ro")
        self.assertEqual(out["error"], "read_only_vault")
        self.assertTrue((self.root / "areas/threads/good.md").exists())

    def test_read_note_still_works(self):
        out = self.ops.read_note(path="areas/threads/good.md", vault="ro")
        self.assertTrue(out["ok"], out)

    def test_writable_vault_is_unaffected(self):
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "rw",
                    "vaults": {
                        "rw": {
                            "root": str(self.root),
                            "index": str(Path(self.tmp.name) / "rw.db"),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        out = self.ops.write_note("areas/threads/new.md", "# new\n", vault="rw")
        self.assertTrue(out["ok"], out)


if __name__ == "__main__":
    unittest.main()
