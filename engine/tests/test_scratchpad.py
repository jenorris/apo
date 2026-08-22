"""Scratchpad store / validate / merge / promote (no Ollama)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import ops, scratchpad, vaults
from apo_engine.scratchpad_merge import merge_buffers
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

        frag = scratchpad.scratchpad_op(
            "read",
            session_id=sid,
            include=["fragment"],
            json_path="$.count",
        )
        self.assertEqual(frag["fragment"], 2)

        status = scratchpad.scratchpad_op("status", session_id=sid)
        self.assertEqual(status["state"], "STAGED")

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
            format="markdown",
            content=f"# Big\n\n{big}\n",
        )
        raw = scratchpad.scratchpad_op(
            "read",
            session_id=created["session_id"],
            include=["buffer"],
        )
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
        raw = scratchpad.scratchpad_op(
            "read", session_id=created["session_id"], include=["buffer"]
        )
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


class ScratchpadValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spill = self.root / "spill"
        self.spill.mkdir()
        self.vault_a = self.root / "vault-a"
        self.vault_b = self.root / "vault-b"
        for v, vid in ((self.vault_a, "alpha"), (self.vault_b, "beta")):
            (v / "system" / "schemas").mkdir(parents=True)
            (v / "system" / "contracts").mkdir(parents=True)
            (v / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
                f"vault_id: {vid}\n", encoding="utf-8"
            )
        (self.vault_a / "system" / "schemas" / "widget.schema.json").write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "required": ["name", "qty"],
                    "properties": {
                        "name": {"type": "string"},
                        "qty": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                    "x-apo-handoff": ["name", "qty"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.vault_b / "system" / "schemas" / "widget.schema.json").write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "required": ["name", "qty", "color"],
                    "properties": {
                        "name": {"type": "string"},
                        "qty": {"type": "integer"},
                        "color": {"type": "string"},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.vault_a / "system" / "contracts" / "okf-contract.schema.yaml").write_text(
            "type_profiles:\n"
            "  Plan:\n"
            "    todos:\n"
            "      item_status: [pending, in_progress, completed, cancelled]\n"
            "    note_status: [draft, active, blocked, done, abandoned]\n",
            encoding="utf-8",
        )
        reg = self.root / "vaults.json"
        reg.write_text(
            json.dumps(
                {
                    "default": "alpha",
                    "vaults": {
                        "ignored-a": {"root": str(self.vault_a), "index": str(self.root / "a.db")},
                        "ignored-b": {"root": str(self.vault_b), "index": str(self.root / "b.db")},
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
        os.environ["APO_NOTES_ROOT"] = str(self.vault_a)
        os.environ["APO_INDEX"] = str(self.root / "index.db")
        os.environ["APO_COLLECTION"] = "sp_a"
        vaults._vault_id_cache.clear()

    def tearDown(self):
        vaults._vault_id_cache.clear()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def test_bind_schema_and_validate(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"name": "bolt"},
            vault="alpha",
            schema_path="system/schemas/widget.schema.json",
        )
        self.assertFalse(created.get("valid", True))
        sid = created["session_id"]
        patched = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "set_field", "field": "qty", "value": 3}],
        )
        self.assertTrue(patched["ok"])
        self.assertTrue(patched.get("valid"))

        handoff = scratchpad.scratchpad_op(
            "read", session_id=sid, view="handoff"
        )
        self.assertEqual(handoff["handoff"].get("name"), "bolt")
        self.assertEqual(handoff["handoff"].get("qty"), 3)

    def test_foreign_schema_default_refuse(self):
        (self.vault_a / "other.schema.json").write_text(
            '{"type":"object"}\n', encoding="utf-8"
        )
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={},
            vault="alpha",
            schema_path="other.schema.json",
        )
        self.assertFalse(created["ok"])
        allowed = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={},
            vault="alpha",
            schema_path="other.schema.json",
            allow_foreign_schema=True,
        )
        self.assertTrue(allowed["ok"])

    def test_cross_vault_schema_blocks_commit(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"name": "x", "qty": 1, "color": "red"},
            vault="beta",
            schema_path="system/schemas/widget.schema.json",
        )
        self.assertTrue(created.get("valid"), created)
        sid = created["session_id"]
        blocked = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="notes/widget.json",
            vault="alpha",
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "cross_vault_schema")

        # write_note must enforce the same pin
        blocked_write = ops.write_note(
            "notes/via-write.json",
            scratchpad=sid,
            vault="alpha",
        )
        self.assertFalse(blocked_write["ok"])
        self.assertEqual(blocked_write["error"], "cross_vault_schema")

        # Opt-in relaxes the pin; commit then revalidates against destination vault
        # (alpha schema forbids `color`) → validation_failed.
        failed = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="notes/widget.json",
            vault="alpha",
            allow_cross_vault_schema=True,
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed.get("error"), "validation_failed")

        # Same-vault commit succeeds
        ok = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="notes/widget.json",
            vault="beta",
        )
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["state"], "PROMOTED")

    def test_type_profile_plan(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={
                "status": "draft",
                "todos": [{"id": "1", "content": "do", "status": "pending"}],
            },
            vault="alpha",
            schema_type="Plan",
        )
        self.assertTrue(created.get("valid"))
        bad = scratchpad.scratchpad_op(
            "patch",
            session_id=created["session_id"],
            ops=[{"op": "set_field", "field": "status", "value": "nope"}],
        )
        self.assertFalse(bad.get("valid"))

    def test_plan_status_open_hint(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"status": "open", "todos": []},
            vault="alpha",
            schema_type="Plan",
        )
        diags = created.get("diagnostics") or []
        self.assertTrue(any(d.get("code") == "PROFILE_STATUS" for d in diags))
        status_diag = next(d for d in diags if d.get("code") == "PROFILE_STATUS")
        self.assertIn("active", status_diag.get("hint", ""))

    def test_status_includes_profile_allowlists(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"status": "draft", "todos": []},
            vault="alpha",
            schema_type="Plan",
        )
        sid = created["session_id"]
        st = scratchpad.scratchpad_op("status", session_id=sid)
        allow = st.get("profile_allowlists") or {}
        self.assertEqual(
            allow.get("note_status"),
            ["draft", "active", "blocked", "done", "abandoned"],
        )
        self.assertIn("pending", allow.get("todo_status") or [])

    def test_dual_bind_schema_path_and_type(self):
        schema_file = self.vault_a / "system" / "schemas" / "widget.schema.json"
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        data["properties"]["status"] = {"type": "string"}
        data["properties"]["todos"] = {"type": "array"}
        schema_file.write_text(json.dumps(data) + "\n", encoding="utf-8")
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={
                "name": "bolt",
                "qty": 2,
                "status": "draft",
                "todos": [{"id": "1", "content": "ship", "status": "pending"}],
            },
            vault="alpha",
            schema_path="system/schemas/widget.schema.json",
            schema_type="Plan",
        )
        self.assertTrue(created.get("valid"), created)
        sid = created["session_id"]
        bad = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "delete_field", "field": "qty"}],
        )
        self.assertFalse(bad.get("valid"))
        diags = bad.get("diagnostics") or []
        self.assertTrue(any(d.get("code") == "SCHEMA_ERROR" for d in diags))

    def test_schema_hash_drift_warning(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"name": "bolt", "qty": 1},
            vault="alpha",
            schema_path="system/schemas/widget.schema.json",
        )
        sid = created["session_id"]
        schema_file = self.vault_a / "system" / "schemas" / "widget.schema.json"
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        data["properties"]["note"] = {"type": "string"}
        schema_file.write_text(json.dumps(data) + "\n", encoding="utf-8")
        validated = scratchpad.scratchpad_op("validate", session_id=sid, vault="alpha")
        self.assertTrue(validated.get("valid"))
        diags = validated.get("diagnostics") or []
        self.assertTrue(any(d.get("code") == "SCHEMA_CHANGED" for d in diags))


class ScratchpadMergeTests(unittest.TestCase):
    def test_non_overlapping_sections_merge(self):
        base = "# A\nbase-a\n\n# B\nbase-b\n"
        ours = "# A\nours-a\n\n# B\nbase-b\n"
        theirs = "# A\nbase-a\n\n# B\ntheirs-b\n"
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertIn("ours-a", merged)
        self.assertIn("theirs-b", merged)

    def test_overlapping_section_conflict(self):
        base = "# A\nbase\n"
        ours = "# A\nours\n"
        theirs = "# A\ntheirs\n"
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertIsNone(merged)
        self.assertTrue(conflicts)

    def test_append_eof_new_section_merges_without_duplication(self):
        """append_eof adds a section that must not appear twice after FM-only trunk edits."""
        base = "---\ntitle: T\n---\n# Hello\none\n"
        ours = base + "\n## Scratchpad-only section\n\nAdded only in scratchpad.\n"
        theirs = "---\ntitle: T\nstatus: active\n---\n# Hello\none\n"
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertEqual(merged.count("Scratchpad-only section"), 1)

    def test_top_level_eof_section_merges_without_duplication(self):
        base = "# A\nbase-a\n\n# B\nbase-b\n"
        ours = base + "\n# C\nnew-c\n"
        theirs = "# A\nbase-a\n\n# B\ntrunk-b\n"
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertEqual(merged.count("# C"), 1)
        self.assertIn("trunk-b", merged)
        self.assertIn("new-c", merged)

    def test_frontmatter_delete_conflict(self):
        base = "---\nstatus: open\ntitle: T\n---\n# Body\nx\n"
        ours = "---\nstatus: done\ntitle: T\n---\n# Body\nx\n"
        theirs = "---\ntitle: T\n---\n# Body\nx\n"  # trunk deleted status
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertIsNone(merged)
        self.assertTrue(any("status" in c.get("path", "") for c in conflicts))

    def test_frontmatter_field_merge(self):
        base = "---\nstatus: open\ntitle: T\n---\n# Body\nx\n"
        ours = "---\nstatus: done\ntitle: T\n---\n# Body\nx\n"
        theirs = "---\nstatus: open\ntitle: Other\n---\n# Body\nx\n"
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertIn("status: done", merged)
        self.assertIn("title: Other", merged)

    def test_frontmatter_one_sided_preserves_comments(self):
        """Only ours edits FM; trunk edits body — FM text stays comment-faithful."""
        base = (
            "---\n"
            "title: Test     # display name\n"
            "# status is set by the watcher\n"
            "status: open\n"
            "---\n"
            "# Body\n"
            "x\n"
        )
        ours = (
            "---\n"
            "title: Test     # display name\n"
            "# status is set by the watcher\n"
            "status: done\n"
            "---\n"
            "# Body\n"
            "x\n"
        )
        theirs = (
            "---\n"
            "title: Test     # display name\n"
            "# status is set by the watcher\n"
            "status: open\n"
            "---\n"
            "# Body\n"
            "trunk\n"
        )
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertIn("status: done", merged)
        self.assertIn("# status is set by the watcher", merged)
        self.assertIn("# display name", merged)
        self.assertIn("trunk", merged)

    def test_frontmatter_mixed_keys_preserve_comments(self):
        """Ours edits status, theirs edits title — comments on both keys survive."""
        base = (
            "---\n"
            "title: Test     # display name\n"
            "# status is set by the watcher\n"
            "status: open\n"
            "---\n"
            "# Body\n"
            "x\n"
        )
        ours = (
            "---\n"
            "title: Test     # display name\n"
            "# status is set by the watcher\n"
            "status: done\n"
            "---\n"
            "# Body\n"
            "x\n"
        )
        theirs = (
            "---\n"
            "title: Other     # display name\n"
            "# status is set by the watcher\n"
            "status: open\n"
            "---\n"
            "# Body\n"
            "x\n"
        )
        merged, conflicts = merge_buffers(fmt="markdown", base=base, ours=ours, theirs=theirs)
        self.assertEqual(conflicts, [])
        assert merged is not None
        self.assertIn("status: done", merged)
        self.assertIn("title: Other", merged)
        self.assertIn("# status is set by the watcher", merged)
        self.assertIn("# display name", merged)


class ScratchpadCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spill = self.root / "spill"
        self.spill.mkdir()
        self.vault = self.root / "vault"
        (self.vault / "areas").mkdir(parents=True)
        (self.vault / "system" / "contracts").mkdir(parents=True)
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
        note = self.vault / "areas" / "thread.md"
        note.write_text(
            "---\nstatus: open\n---\n# Alpha\none\n\n# Beta\ntwo\n",
            encoding="utf-8",
        )
        reg = self.root / "vaults.json"
        reg.write_text(
            json.dumps(
                {
                    "default": "work",
                    "vaults": {
                        "ignored": {"root": str(self.vault), "index": str(self.root / "work.db")},
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

    def test_checkout_patch_commit_merge(self):
        co = scratchpad.scratchpad_op(
            "checkout",
            vault="work",
            vault_path="areas/thread.md",
        )
        self.assertTrue(co["ok"], co)
        sid = co["session_id"]
        scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "replace_section", "heading": "Alpha", "text": "patched-a\n"}],
        )
        # Concurrent trunk edit to Beta
        trunk = self.vault / "areas" / "thread.md"
        trunk.write_text(
            "---\nstatus: open\n---\n# Alpha\none\n\n# Beta\ntrunk-b\n",
            encoding="utf-8",
        )
        committed = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="areas/thread.md",
            vault="work",
        )
        self.assertTrue(committed["ok"], committed)
        self.assertEqual(committed["state"], "PROMOTED")
        text = trunk.read_text(encoding="utf-8")
        self.assertIn("patched-a", text)
        self.assertIn("trunk-b", text)

        # PROMOTED read-through
        again = scratchpad.scratchpad_op(
            "read", session_id=sid, include=["buffer"]
        )
        self.assertIn("patched-a", again["buffer"])

        denied = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "set_field", "field": "status", "value": "done"}],
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "promoted")

        # duplicate is the escape hatch this message points to — works from PROMOTED.
        cloned = scratchpad.scratchpad_op("duplicate", session_id=sid)
        self.assertTrue(cloned["ok"], cloned)
        self.assertEqual(cloned["state"], "ACTIVE")
        self.assertEqual(cloned["cloned_from"], sid)
        self.assertNotEqual(cloned["session_id"], sid)
        clone_id = cloned["session_id"]

        # The clone is independently mutable and carries the template's content forward.
        allowed = scratchpad.scratchpad_op(
            "patch",
            session_id=clone_id,
            ops=[{"op": "set_field", "field": "status", "value": "done"}],
        )
        self.assertTrue(allowed["ok"], allowed)

        # Committing the clone to a different path never touches the original trunk file.
        variant_committed = scratchpad.scratchpad_op(
            "commit",
            session_id=clone_id,
            destination_path="areas/variant.md",
            vault="work",
        )
        self.assertTrue(variant_committed["ok"], variant_committed)
        variant_text = (self.vault / "areas" / "variant.md").read_text(encoding="utf-8")
        self.assertIn("status: done", variant_text)
        self.assertIn("patched-a", variant_text)
        # Original trunk (from the earlier commit) is untouched by the clone's edits.
        self.assertNotIn("status: done", trunk.read_text(encoding="utf-8"))

        # Original session is still the same denied/PROMOTED state — duplicate didn't mutate it.
        still_denied = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "set_field", "field": "status", "value": "nope"}],
        )
        self.assertFalse(still_denied["ok"])
        self.assertEqual(still_denied["error"], "promoted")

    def test_duplicate_active_session_forks_independently(self):
        co = scratchpad.scratchpad_op("checkout", vault="work", vault_path="areas/thread.md")
        sid = co["session_id"]
        scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[{"op": "replace_section", "heading": "Alpha", "text": "from-original\n"}],
        )
        cloned = scratchpad.scratchpad_op("duplicate", session_id=sid)
        self.assertTrue(cloned["ok"], cloned)
        clone_id = cloned["session_id"]
        self.assertNotEqual(clone_id, sid)

        clone_buf = scratchpad.scratchpad_op("read", session_id=clone_id, include=["buffer"])
        self.assertIn("from-original", clone_buf["buffer"])

        # Patching the clone must not leak back into the source session's buffer.
        scratchpad.scratchpad_op(
            "patch",
            session_id=clone_id,
            ops=[{"op": "replace_section", "heading": "Alpha", "text": "from-clone\n"}],
        )
        original_buf = scratchpad.scratchpad_op("read", session_id=sid, include=["buffer"])
        self.assertIn("from-original", original_buf["buffer"])
        self.assertNotIn("from-clone", original_buf["buffer"])

    def test_duplicate_missing_session_not_found(self):
        out = scratchpad.scratchpad_op("duplicate", session_id="does-not-exist")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "not_found")

    def test_duplicate_requires_session_id(self):
        out = scratchpad.scratchpad_op("duplicate")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_checkout_rejects_path_escape(self):
        outside = self.root / "secret.txt"
        outside.write_text("nope\n", encoding="utf-8")
        bad = scratchpad.scratchpad_op(
            "checkout",
            vault="work",
            vault_path="../secret.txt",
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_path")

    def test_write_note_scratchpad_promote(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="markdown",
            content="# New\nhello\n",
        )
        sid = created["session_id"]
        written = ops.write_note(
            "areas/from-spill.md",
            scratchpad=sid,
            vault="work",
        )
        self.assertTrue(written["ok"], written)
        self.assertEqual(written.get("scratchpad_state"), "PROMOTED")
        self.assertTrue((self.vault / "areas" / "from-spill.md").is_file())
        meta, _ = load_session(sid)  # type: ignore[misc]
        self.assertEqual(meta.state, "PROMOTED")

    def test_patch_note_scratchpad_promote(self):
        co = scratchpad.scratchpad_op(
            "checkout",
            vault="work",
            vault_path="areas/thread.md",
        )
        self.assertTrue(co["ok"], co)
        sid = co["session_id"]
        trunk = self.vault / "areas" / "thread.md"
        trunk.write_text(
            "---\nstatus: open\n---\n# Alpha\none\n\n# Beta\ntrunk-b\n",
            encoding="utf-8",
        )
        patched = ops.patch_entry(
            path="areas/thread.md",
            ops=[{"op": "replace_section", "heading": "Alpha", "text": "via-patch\n"}],
            scratchpad=sid,
            vault="work",
        )
        self.assertTrue(patched["ok"], patched)
        self.assertEqual(patched.get("scratchpad_state"), "PROMOTED")
        text = trunk.read_text(encoding="utf-8")
        self.assertIn("via-patch", text)
        self.assertIn("trunk-b", text)
        meta, _ = load_session(sid)  # type: ignore[misc]
        self.assertEqual(meta.state, "PROMOTED")

    def test_patch_note_rejects_place_with_path(self):
        out = ops.patch_note(
            "areas/thread.md",
            [{"op": "place", "src": "areas/thread.md", "dst": "areas/other.md"}],
            vault="work",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")
        self.assertIn("place-only", out.get("message") or "")

    def test_patch_note_scratchpad_rejects_place(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="markdown",
            content="# X\ny\n",
        )
        sid = created["session_id"]
        out = ops.patch_entry(
            path="areas/from-spill.md",
            ops=[{"op": "place", "src": "a.md", "dst": "b.md"}],
            scratchpad=sid,
            vault="work",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_write_note_scratchpad_ignores_empty_content(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="markdown",
            content="# Only\nspill\n",
        )
        sid = created["session_id"]
        written = ops.write_note(
            "areas/empty-content-promote.md",
            content="",
            scratchpad=sid,
            vault="work",
        )
        self.assertTrue(written["ok"], written)
        text = (self.vault / "areas" / "empty-content-promote.md").read_text(encoding="utf-8")
        self.assertIn("spill", text)

    def test_commit_json_catalog_writes_raw(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="json",
            content={"status": "draft", "todos": []},
            vault="work",
            schema_type="Plan",
        )
        sid = created["session_id"]
        committed = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="areas/plan-handoff.json",
            vault="work",
        )
        self.assertTrue(committed["ok"], committed)
        raw = (self.vault / "areas/plan-handoff.json").read_text(encoding="utf-8")
        self.assertFalse(raw.lstrip().startswith("---"))
        parsed = json.loads(raw)
        self.assertEqual(parsed.get("status"), "draft")

    def test_commit_mmd_writes_raw(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="mmd",
            content="flowchart LR\n  A[Alpha] --> B[Beta]\n",
            vault="work",
        )
        sid = created["session_id"]
        committed = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="diagrams/workshop/diagram.mmd",
            vault="work",
        )
        self.assertTrue(committed["ok"], committed)
        raw = (self.vault / "diagrams/workshop/diagram.mmd").read_text(encoding="utf-8")
        self.assertIn("flowchart LR", raw)
        self.assertFalse(raw.lstrip().startswith("---"))

    def test_append_note_scratchpad_promote(self):
        target = self.vault / "areas" / "host.md"
        target.write_text("# Host\noriginal\n", encoding="utf-8")
        created = scratchpad.scratchpad_op(
            "create",
            format="markdown",
            content="# Spill\nfrom scratchpad\n",
        )
        sid = created["session_id"]
        appended = ops.append_note(
            "areas/host.md",
            scratchpad=sid,
            vault="work",
        )
        self.assertTrue(appended["ok"], appended)
        self.assertEqual(appended.get("scratchpad_state"), "PROMOTED")
        text = target.read_text(encoding="utf-8")
        self.assertIn("original", text)
        self.assertIn("from scratchpad", text)
        meta, _ = load_session(sid)  # type: ignore[misc]
        self.assertEqual(meta.state, "PROMOTED")

    def test_append_note_scratchpad_xor_text(self):
        created = scratchpad.scratchpad_op(
            "create",
            format="markdown",
            content="# Spill\n",
        )
        sid = created["session_id"]
        bad = ops.append_note(
            "areas/host.md",
            text="nope",
            scratchpad=sid,
            vault="work",
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")

    def test_checkout_append_eof_commit_merge(self):
        note = self.vault / "areas" / "eof-thread.md"
        note.write_text(
            "---\nstatus: open\n---\n# Alpha\none\n\n# Beta\ntwo\n",
            encoding="utf-8",
        )
        co = scratchpad.scratchpad_op(
            "checkout",
            vault="work",
            vault_path="areas/eof-thread.md",
        )
        self.assertTrue(co["ok"], co)
        sid = co["session_id"]
        patched = scratchpad.scratchpad_op(
            "patch",
            session_id=sid,
            ops=[
                {
                    "op": "append_eof",
                    "text": "\n## Gamma\n\nscratchpad-only\n",
                }
            ],
        )
        self.assertTrue(patched["ok"], patched)
        note.write_text(
            "---\nstatus: done\n---\n# Alpha\none\n\n# Beta\ntwo\n",
            encoding="utf-8",
        )
        committed = scratchpad.scratchpad_op(
            "commit",
            session_id=sid,
            destination_path="areas/eof-thread.md",
            vault="work",
        )
        self.assertTrue(committed["ok"], committed)
        text = note.read_text(encoding="utf-8")
        self.assertEqual(text.count("Gamma"), 1)
        self.assertIn("status: done", text)
        self.assertIn("scratchpad-only", text)

class ScratchpadRpcRouteTests(unittest.TestCase):
    def test_rpc_route_registered(self):
        from apo_engine import rpc

        self.assertIn(("POST", "/v1/scratchpad"), rpc._ROUTES)
        self.assertIn(("GET", "/v1/scratchpad"), rpc._ROUTES)


if __name__ == "__main__":
    unittest.main()
