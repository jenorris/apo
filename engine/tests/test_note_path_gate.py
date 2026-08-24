"""Mutator suffix gate — reject non-note paths before OKF stamp."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import okf, ops


class NotePathGateBase(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT", "APO_OKF_SPEC_TYPE", "APO_VAULTS"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: meta\npurpose: note path gate test\n",
            encoding="utf-8",
        )
        (self.root / "system" / "contracts" / "okf-contract.schema.yaml").write_text(
            """
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
path_rules:
  - match: "areas/threads/**/*.md"
    enforcement: soft
    okf_type: Thread
  - match: "system/scripts/**/*.py"
    enforcement: soft
    okf_type: System
""",
            encoding="utf-8",
        )
        (self.root / "areas" / "threads").mkdir(parents=True)
        (self.root / "system" / "scripts").mkdir(parents=True)
        self.script = self.root / "system" / "scripts" / "bot.py"
        self.script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
        self.md = self.root / "areas" / "threads" / "good.md"
        self.md.write_text("# Good\n", encoding="utf-8")

        self.vaults_file = Path(self.tmp.name) / "vaults.json"
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "meta",
                    "vaults": {
                        "meta": {
                            "root": str(self.root),
                            "index": str(Path(self.tmp.name) / "meta.db"),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        os.environ["APO_VAULTS"] = str(self.vaults_file)
        from apo_engine import vaults as _vaults

        _ = _vaults

    def tearDown(self):
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class NotePathGateTests(NotePathGateBase):
    def test_patch_note_rejects_py(self):
        out = ops.patch_note(
            "system/scripts/bot.py",
            [{"op": "append_section", "heading": "Log", "text": "x", "level": 2}],
            vault="meta",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unsupported_format")
        self.assertTrue(self.script.read_text(encoding="utf-8").startswith("#!/usr/bin/env"))

    def test_write_note_rejects_py(self):
        out = ops.write_note("system/scripts/new.py", "#!/bin/sh\n", vault="meta")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unsupported_format")
        self.assertFalse((self.root / "system" / "scripts" / "new.py").exists())

    def test_append_note_rejects_py(self):
        out = ops.append_note("system/scripts/bot.py", text="more", vault="meta")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unsupported_format")

    def test_delete_note_rejects_py(self):
        out = ops.delete_note("system/scripts/bot.py", vault="meta")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unsupported_format")
        self.assertTrue(self.script.exists())

    def test_patch_notes_batch_rejects_py_item(self):
        out = ops.patch_notes(
            [
                {
                    "path": "system/scripts/bot.py",
                    "ops": [{"op": "replace_text", "find": "ok", "replace": "nope"}],
                },
                {
                    "path": "areas/threads/good.md",
                    "ops": [{"op": "set_field", "field": "status", "value": "active"}],
                },
            ],
            vault="meta",
        )
        self.assertFalse(out["ok"])
        results = out.get("results") or []
        self.assertEqual(len(results), 2)
        py_item = next(r for r in results if r.get("path") == "system/scripts/bot.py")
        md_item = next(r for r in results if r.get("path") == "areas/threads/good.md")
        self.assertEqual(py_item.get("error"), "unsupported_format")
        self.assertTrue(md_item.get("ok"))

    def test_move_note_rejects_py_src_or_dst(self):
        out_src = ops.move_note(
            "system/scripts/bot.py",
            "system/scripts/bot.bak.py",
            vault="meta",
        )
        self.assertEqual(out_src.get("error"), "unsupported_format")
        out_dst = ops.move_note(
            "areas/threads/good.md",
            "system/scripts/good.py",
            vault="meta",
        )
        self.assertEqual(out_dst.get("error"), "unsupported_format")

    def test_md_soft_stamp_still_works(self):
        out = ops.write_note(
            "areas/threads/new.md",
            "# New thread\n",
            vault="meta",
        )
        self.assertTrue(out.get("ok"), out)
        text = (self.root / "areas" / "threads" / "new.md").read_text(encoding="utf-8")
        self.assertIn("okf_type:", text)
        self.assertIn("type:", text)


if __name__ == "__main__":
    unittest.main()
