"""Mutator suffix gate — reject non-note paths before OKF stamp."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import config, okf, ops


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

    def test_all_mutators_reject_contract_schema(self):
        rel = "system/contracts/okf-contract.schema.yaml"
        original = (self.root / rel).read_text(encoding="utf-8")
        calls = (
            lambda: ops.write_note(rel, "changed\n", vault="meta"),
            lambda: ops.append_note(rel, text="changed", vault="meta"),
            lambda: ops.patch_note(
                rel, [{"op": "replace_text", "find": "okf_version", "replace": "changed"}], vault="meta"
            ),
            lambda: ops.delete_note(rel, vault="meta"),
            lambda: ops.move_note(rel, "areas/threads/moved.yaml", vault="meta"),
        )
        for call in calls:
            with self.subTest(call=call):
                out = call()
                self.assertFalse(out["ok"], out)
                self.assertEqual(out["error"], "protected_control_plane")
        self.assertEqual((self.root / rel).read_text(encoding="utf-8"), original)

    def test_legacy_contract_schema_is_protected(self):
        legacy = self.root / "system" / "config" / "git-contract.schema.yaml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("git_contract_version: '0.1'\n", encoding="utf-8")
        out = ops.write_note(
            "system/config/git-contract.schema.yaml", "changed\n", vault="meta"
        )
        self.assertEqual(out.get("error"), "protected_control_plane")

    def test_normalized_contract_schema_aliases_are_protected_for_all_mutators(self):
        original = (
            self.root / "system" / "contracts" / "okf-contract.schema.yaml"
        ).read_text(encoding="utf-8")
        aliases = (
            "foo/../system/contracts/okf-contract.schema.yaml",
            r"foo\..\system\contracts\okf-contract.schema.yaml",
        )
        for alias in aliases:
            calls = (
                lambda alias=alias: ops.write_note(alias, "changed\n", vault="meta"),
                lambda alias=alias: ops.append_note(alias, text="changed", vault="meta"),
                lambda alias=alias: ops.patch_note(
                    alias,
                    [{"op": "replace_text", "find": "okf_version", "replace": "changed"}],
                    vault="meta",
                ),
                lambda alias=alias: ops.delete_note(alias, vault="meta"),
                lambda alias=alias: ops.move_note(
                    alias, "areas/threads/moved.yaml", vault="meta"
                ),
                lambda alias=alias: ops.move_note(
                    "areas/threads/good.md", alias, vault="meta"
                ),
                lambda alias=alias: ops.patch_notes(
                    [
                        {
                            "path": alias,
                            "ops": [
                                {
                                    "op": "replace_text",
                                    "find": "okf_version",
                                    "replace": "changed",
                                }
                            ],
                        }
                    ],
                    vault="meta",
                ),
            )
            for call in calls:
                with self.subTest(alias=alias, call=call):
                    out = call()
                    self.assertFalse(out["ok"], out)
                    if "results" in out:
                        self.assertEqual(
                            out["results"][0]["error"], "protected_control_plane"
                        )
                    else:
                        self.assertEqual(out["error"], "protected_control_plane")
        self.assertEqual(
            (
                self.root / "system" / "contracts" / "okf-contract.schema.yaml"
            ).read_text(encoding="utf-8"),
            original,
        )
        self.assertTrue(self.md.exists())

    def test_normalization_does_not_weaken_traversal_rejection(self):
        out = ops.write_note("../outside.md", "# Outside\n", vault="meta")
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"], "bad_path")
        self.assertFalse((self.root.parent / "outside.md").exists())

    def test_canonical_contract_targets_are_protected_for_mutators(self):
        contract = self.root / "system" / "contracts" / "okf-contract.schema.yaml"
        legacy = self.root / "system" / "config" / "git-contract.schema.yaml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("git_contract_version: '0.1'\n", encoding="utf-8")
        originals = {
            contract: contract.read_text(encoding="utf-8"),
            legacy: legacy.read_text(encoding="utf-8"),
        }
        host_note = self.root.parent / "host.md"
        host_note.write_text("# Host\n", encoding="utf-8")
        aliases: list[tuple[Path, str]] = []
        for target in (contract, legacy):
            link = self.root / f"{target.parent.name}-alias"
            link.symlink_to(target.parent, target_is_directory=True)
            aliases.extend(
                (
                    (target, str(target)),
                    (target, f"{link.name}/{target.name}"),
                )
            )

        for target, alias in aliases:
            calls = (
                ("write", lambda alias=alias: ops.write_note(alias, "changed\n", vault="meta")),
                (
                    "patch",
                    lambda alias=alias: ops.patch_note(
                        alias,
                        [{"op": "replace_text", "find": "_version", "replace": "_changed"}],
                        vault="meta",
                    ),
                ),
                ("delete", lambda alias=alias: ops.delete_note(alias, vault="meta")),
                (
                    "move source",
                    lambda alias=alias: ops.move_note(
                        alias, "areas/threads/moved.yaml", vault="meta"
                    ),
                ),
                (
                    "move destination",
                    lambda alias=alias: ops.move_note(
                        "areas/threads/good.md", alias, vault="meta"
                    ),
                ),
                (
                    "copy destination",
                    lambda alias=alias: ops.place_note(
                        str(host_note), alias, vault="meta"
                    ),
                ),
                (
                    "batch patch",
                    lambda alias=alias: ops.patch_notes(
                        [
                            {
                                "path": alias,
                                "ops": [
                                    {
                                        "op": "replace_text",
                                        "find": "_version",
                                        "replace": "_changed",
                                    }
                                ],
                            }
                        ],
                        vault="meta",
                    ),
                ),
            )
            for action, call in calls:
                with self.subTest(target=target, alias=alias, action=action):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(originals[target], encoding="utf-8")
                    self.md.parent.mkdir(parents=True, exist_ok=True)
                    self.md.write_text("# Good\n", encoding="utf-8")
                    moved = self.root / "areas" / "threads" / "moved.yaml"
                    moved.unlink(missing_ok=True)
                    with unittest.mock.patch.object(
                        config, "SEND_ALLOW_ROOTS", str(host_note.parent)
                    ):
                        out = call()
                    self.assertFalse(out["ok"], out)
                    if "results" in out:
                        self.assertEqual(
                            out["results"][0]["error"], "protected_control_plane"
                        )
                    else:
                        self.assertEqual(out["error"], "protected_control_plane")
                    self.assertEqual(
                        target.read_text(encoding="utf-8"), originals[target]
                    )
                    self.assertTrue(self.md.exists())
                    self.assertFalse(moved.exists())

    def test_safe_symlink_writes_and_contract_reads_remain_available(self):
        safe_link = self.root / "safe-alias"
        safe_link.symlink_to(self.md.parent, target_is_directory=True)
        written = ops.write_note(
            "safe-alias/linked.md", "# Linked\n", vault="meta"
        )
        self.assertTrue(written["ok"], written)
        self.assertTrue((self.md.parent / "linked.md").exists())

        contracts_link = self.root / "contracts-read"
        contracts_link.symlink_to(
            self.root / "system" / "contracts", target_is_directory=True
        )
        read = ops.read_note(
            "contracts-read/okf-contract.schema.yaml", vault="meta", raw=True
        )
        self.assertTrue(read["ok"], read)
        self.assertIn("okf_version", read["content"])

    def test_failed_delete_does_not_enqueue_purge(self):
        with unittest.mock.patch.object(Path, "unlink", side_effect=OSError("denied")):
            with unittest.mock.patch.object(ops, "_enqueue_purge") as enqueue:
                with self.assertRaisesRegex(OSError, "denied"):
                    ops.delete_note("areas/threads/good.md", vault="meta")
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
