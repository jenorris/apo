"""Scratchpad create/patch/read/commit/discard (no Ollama)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import ops, scratchpad, vaults
from apo_engine.scratchpad_store import load_session


class ScratchpadWorkshopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spill = self.root / "spill"
        self.spill.mkdir()
        self._env = os.environ.get("APO_SCRATCHPADS_ROOT")
        os.environ["APO_SCRATCHPADS_ROOT"] = str(self.spill)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("APO_SCRATCHPADS_ROOT", None)
        else:
            os.environ["APO_SCRATCHPADS_ROOT"] = self._env
        self.tmp.cleanup()

    def test_create_patch_json_vault_free(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"title": "hi", "count": 1},
        )
        self.assertTrue(created["ok"])
        sid = created["session_id"]
        self.assertEqual(created["format"], "json")
        self.assertNotIn("buffer", created)

        patched = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "set_field", "field": "count", "value": 2}],
        )
        self.assertTrue(patched["ok"])
        self.assertEqual(patched["state"], "STAGED")
        self.assertNotIn("buffer", patched)

        read = scratchpad.scratchpad_op("read", session_id=sid)
        self.assertIn("buffer", read)
        self.assertIn('"count": 2', read["buffer"])

        discarded = scratchpad.scratchpad_op("discard", session_id=sid)
        self.assertTrue(discarded["ok"])
        self.assertIsNone(load_session(sid))

    def test_discard_idempotent(self):
        created = scratchpad.scratchpad_op("create", format="json", content={})
        sid = created["session_id"]
        self.assertTrue(scratchpad.scratchpad_op("discard", session_id=sid)["ok"])
        again = scratchpad.scratchpad_op("discard", session_id=sid)
        self.assertTrue(again["ok"])
        self.assertIsNone(load_session(sid))

    def test_read_buffer_truncation_tip(self):
        big = "x" * (9 * 1024)
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"body": big},
        )
        raw = scratchpad.scratchpad_op("read", session_id=created["session_id"])
        self.assertIn("tip", raw)
        self.assertLess(len(raw["buffer"].encode("utf-8")), 9 * 1024)

    def test_ill_formed_json_keeps_raw(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content='{"broken":',
        )
        self.assertTrue(created["ok"])
        diags = created.get("diagnostics") or []
        self.assertTrue(any(d.get("code") == "JSON_PARSE" for d in diags))
        raw = scratchpad.scratchpad_op("read", session_id=created["session_id"])
        self.assertIn("broken", raw["buffer"])

    def test_patch_rejects_path_on_ops(self):
        created = scratchpad.scratchpad_op("create", format="json", content={})
        bad = scratchpad.scratchpad_op(
            "patch",
            session_id=created["session_id"],
            ops=[{"op": "set_field", "path": "x.md", "field": "a", "value": 1}],
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")

    def test_unsupported_format(self):
        bad = scratchpad.scratchpad_op("create", format="markdown", content="# hi")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")

    def test_unknown_action(self):
        bad = scratchpad.scratchpad_op("validate")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")
        created = scratchpad.scratchpad_op("create", format="json", content={})
        bad2 = scratchpad.scratchpad_op("bind_schema", session_id=created["session_id"])
        self.assertFalse(bad2["ok"])
        self.assertEqual(bad2["error"], "bad_action")


class ScratchpadCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spill = self.root / "spill"
        self.spill.mkdir()
        self.vault = self.root / "vault"
        (self.vault / "inbox").mkdir(parents=True)
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "system" / "schemas").mkdir(parents=True)
        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "vault_id: work\n", encoding="utf-8"
        )
        (self.vault / "system" / "contracts" / "okf-contract.schema.yaml").write_text(
            "type_profiles:\n"
            "  Plan:\n"
            "    todos:\n"
            "      item_status: [pending, in_progress, completed, cancelled]\n"
            "    note_status: [draft, active, blocked, done, abandoned]\n",
            encoding="utf-8",
        )
        (self.vault / "system" / "schemas" / "widget.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                }
            ),
            encoding="utf-8",
        )
        reg = self.root / "vaults.json"
        reg.write_text(
            json.dumps(
                {
                    "default": "work",
                    "vaults": {
                        "work": {"root": str(self.vault), "index": str(self.root / "work.db")},
                    },
                }
            ),
            encoding="utf-8",
        )
        self._env = {
            "APO_SCRATCHPADS_ROOT": os.environ.get("APO_SCRATCHPADS_ROOT"),
            "APO_VAULTS": os.environ.get("APO_VAULTS"),
            "APO_NOTES_ROOT": os.environ.get("APO_NOTES_ROOT"),
            "APO_INDEX": os.environ.get("APO_INDEX"),
            "APO_COLLECTION": os.environ.get("APO_COLLECTION"),
        }
        os.environ["APO_SCRATCHPADS_ROOT"] = str(self.spill)
        os.environ["APO_VAULTS"] = str(reg)
        os.environ["APO_NOTES_ROOT"] = str(self.vault)
        os.environ["APO_INDEX"] = str(self.root / "index.db")
        os.environ["APO_COLLECTION"] = "sp_commit"
        vaults._vault_id_cache.clear()

    def tearDown(self):
        vaults._vault_id_cache.clear()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def test_commit_json_catalog_raw(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"title": "catalog", "n": 1},
        )
        sid = created["session_id"]
        out = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            vault="work",
            destination_path="inbox/scratchpad-plan-test.json",
        )
        self.assertTrue(out["ok"], out)
        dest = self.vault / "inbox" / "scratchpad-plan-test.json"
        self.assertTrue(dest.is_file())
        text = dest.read_text(encoding="utf-8")
        self.assertNotIn("---", text)
        self.assertIn('"title": "catalog"', text)
        self.assertEqual(out["state"], "PROMOTED")

    def test_commit_schema_path_success_and_failure(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"title": "ok"},
        )
        sid = created["session_id"]
        good = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            vault="work",
            destination_path="inbox/widget.json",
            schema_path="system/schemas/widget.schema.json",
        )
        self.assertTrue(good["ok"], good)

        created2 = scratchpad.scratchpad_op("create", format="json", content={})
        bad = scratchpad.scratchpad_op(
            "commit",
            session_id=created2["session_id"],
            vault="work",
            destination_path="inbox/widget2.json",
            schema_path="system/schemas/widget.schema.json",
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "validation_failed")

    def test_commit_schema_type_plan(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={
                "status": "draft",
                "todos": [{"id": "t1", "content": "Test todo", "status": "pending"}],
            },
        )
        out = scratchpad.scratchpad_op(
            "commit",
            session_id=created["session_id"],
            vault="work",
            destination_path="inbox/scratchpad-plan-test.json",
            schema_type="Plan",
        )
        self.assertTrue(out["ok"], out)

    def test_commit_requires_vault_and_destination(self):
        created = scratchpad.scratchpad_op("create", format="json", content={})
        sid = created["session_id"]
        no_vault = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="inbox/x.json",
        )
        self.assertFalse(no_vault["ok"])
        no_dest = scratchpad.scratchpad_op("commit", session_id=sid, vault="work")
        self.assertFalse(no_dest["ok"])

    def test_promoted_patch_denied(self):
        created = scratchpad.scratchpad_op("create", format="json", content={"a": 1})
        sid = created["session_id"]
        scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            vault="work",
            destination_path="inbox/promoted.json",
        )
        bad = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "set_field", "field": "a", "value": 2}],
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "promoted")


class ScratchpadRpcRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spill = self.root / "spill"
        self.spill.mkdir()
        self._env = os.environ.get("APO_SCRATCHPADS_ROOT")
        os.environ["APO_SCRATCHPADS_ROOT"] = str(self.spill)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("APO_SCRATCHPADS_ROOT", None)
        else:
            os.environ["APO_SCRATCHPADS_ROOT"] = self._env
        self.tmp.cleanup()

    def test_rpc_create_read_discard(self):
        from apo_engine import rpc

        created = rpc._scratchpad({"action": "create", "format": "json", "content": {"x": 1}})
        self.assertTrue(created["ok"])
        sid = created["session_id"]
        read = rpc._scratchpad({"action": "read", "session_id": sid})
        self.assertIn("buffer", read)
        discarded = rpc._scratchpad({"action": "discard", "session_id": sid})
        self.assertTrue(discarded["ok"])
