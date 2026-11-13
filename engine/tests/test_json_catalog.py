"""``.json`` catalogs via write_note / patch_note — raw write, CAS, set_field (no Ollama).

Replaces the retired ``scratchpad`` tool's only unique capability (writing a
``.json`` payload into a vault) and gives JSON patching the ``expected_mtime``
guard scratchpad never had.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apo_engine import config, core, deferred, okf, ops, vaults
from apo_engine.json_patch import apply_json_patch
from apo_engine.markdown_patch import PatchError


class JsonCatalogBase(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT", "APO_OKF_SPEC_TYPE", "APO_VAULTS"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "system" / "contracts").mkdir(parents=True)
        (self.root / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: meta\npurpose: json catalog test\n",
            encoding="utf-8",
        )
        (self.root / "inbox").mkdir()
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
        self._enqueued: list[Path] = []
        self._orig_enqueue = ops._enqueue_index
        ops._enqueue_index = lambda b, full: self._enqueued.append(full)

    def tearDown(self):
        ops._enqueue_index = self._orig_enqueue
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _write(self, rel: str = "inbox/widget.json", data: dict | None = None, **kw):
        payload = json.dumps(data if data is not None else {"title": "w", "n": 1}, indent=2) + "\n"
        return ops.write_note(rel, payload, vault="meta", **kw)

    def _disk(self, rel: str = "inbox/widget.json") -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class JsonWriteTests(JsonCatalogBase):
    def test_write_is_raw_no_okf_wrapper(self):
        out = self._write()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "created")
        self.assertEqual(out["catalog_format"], "json")
        text = self._disk()
        self.assertNotIn("---", text)
        self.assertNotIn("okf_type", text)
        self.assertEqual(json.loads(text), {"title": "w", "n": 1})
        self.assertEqual(self._enqueued, [(self.root / "inbox" / "widget.json").resolve()])

    def test_non_object_is_preserved_and_enqueued_for_purge(self):
        raw = '[1,  2]\n'
        out = ops.write_note("inbox/list.json", raw, vault="meta")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["index_status"], "unsupported_json_top_level")
        self.assertEqual((self.root / "inbox/list.json").read_text(), raw)
        self.assertEqual(self._enqueued, [(self.root / "inbox" / "list.json").resolve()])

    def test_write_rejects_invalid_json(self):
        out = ops.write_note("inbox/broken.json", '{"broken":', vault="meta")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "validation_failed")
        self.assertFalse((self.root / "inbox" / "broken.json").exists())

    def _foreign_edit(self, full: Path, data: dict, base_mtime: float) -> None:
        """Simulate another writer: new bytes *and* a later mtime (an mtime-only
        bump with unchanged content is forgiven by the touch-snapshot soft path)."""
        full.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.utime(full, (base_mtime + 5, base_mtime + 5))

    def test_write_cas_guard(self):
        first = self._write()
        full = self.root / "inbox" / "widget.json"
        self._foreign_edit(full, {"title": "foreign"}, first["mtime"])
        stale = self._write(data={"title": "clobber"}, expected_mtime=first["mtime"])
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"], "stale_write")
        self.assertEqual(json.loads(self._disk())["title"], "foreign")

        fresh = self._write(data={"title": "again"}, expected_mtime=full.stat().st_mtime)
        self.assertTrue(fresh["ok"], fresh)
        self.assertEqual(fresh["action"], "overwrote")
        self.assertEqual(json.loads(self._disk())["title"], "again")

    def test_read_note_returns_raw_json(self):
        self._write()
        read = ops.read_note("inbox/widget.json", vault="meta")
        self.assertTrue(read["ok"], read)
        self.assertEqual(json.loads(read["content"]), {"title": "w", "n": 1})

    def test_append_note_rejected(self):
        self._write()
        out = ops.append_note("inbox/widget.json", text="nope", vault="meta")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unsupported_format")


class JsonPatchTests(JsonCatalogBase):
    def test_set_and_delete_field_native_values(self):
        w = self._write(
            data={
                "status": "draft",
                "meta": {"owner": "a", "drop": True},
                "todos": [{"id": "t1", "status": "pending"}, {"id": "t2", "status": "pending"}],
            }
        )
        out = ops.patch_note(
            "inbox/widget.json",
            [
                {"op": "set_field", "field": "status", "value": "active"},
                {"op": "set_field", "field": "meta.owner", "value": "jeremy"},
                {"op": "set_field", "field": "meta.code", "value": "007"},
                {"op": "set_field", "field": "meta.count", "value": 7},
                {"op": "set_field", "field": "todos[id=t2].status", "value": "done"},
                {"op": "delete_field", "field": "meta.drop"},
            ],
            expected_mtime=w["mtime"],
            vault="meta",
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["applied"], 6)
        data = json.loads(self._disk())
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["meta"], {"owner": "jeremy", "code": "007", "count": 7})
        self.assertEqual(data["todos"][1]["status"], "done")
        self.assertNotIn("okf_type", data)
        self.assertEqual(
            self._enqueued,
            [(self.root / "inbox" / "widget.json").resolve()] * 2,
        )

    def test_patch_cas_guard(self):
        w = self._write()
        full = self.root / "inbox" / "widget.json"
        full.write_text(json.dumps({"title": "foreign"}) + "\n", encoding="utf-8")
        os.utime(full, (w["mtime"] + 5, w["mtime"] + 5))
        out = ops.patch_note(
            "inbox/widget.json",
            [{"op": "set_field", "field": "title", "value": "clobber"}],
            expected_mtime=w["mtime"],
            vault="meta",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "stale_write")
        self.assertEqual(json.loads(self._disk())["title"], "foreign")

    def test_markdown_and_table_ops_rejected(self):
        self._write()
        section = ops.patch_note(
            "inbox/widget.json",
            [{"op": "append", "text": "nope", "heading": "Log"}],
            vault="meta",
        )
        self.assertFalse(section["ok"])
        self.assertEqual(section["error"], "unsupported_format")
        table = ops.patch_note(
            "inbox/widget.json",
            [{"op": "append_row", "row": {"a": 1}}],
            vault="meta",
        )
        self.assertFalse(table["ok"])
        self.assertEqual(table["error"], "unsupported_format")
        self.assertEqual(json.loads(self._disk()), {"title": "w", "n": 1})

    def test_patch_rejects_non_object_document(self):
        (self.root / "inbox" / "list.json").write_text("[1, 2]\n", encoding="utf-8")
        out = ops.patch_note(
            "inbox/list.json",
            [{"op": "set_field", "field": "a", "value": 1}],
            vault="meta",
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "invalid_json")

    def test_dry_run_does_not_write(self):
        self._write()
        out = ops.patch_note(
            "inbox/widget.json",
            [{"op": "set_field", "field": "title", "value": "preview"}],
            dry_run=True,
            vault="meta",
        )
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(json.loads(self._disk())["title"], "w")


class ApplyJsonPatchUnitTests(unittest.TestCase):
    def test_strict_rolls_back_whole_batch(self):
        src = '{"a": 1}\n'
        res = apply_json_patch(
            src,
            [
                {"op": "set_field", "field": "a", "value": 2},
                {"op": "append", "text": "x"},
            ],
            strict=True,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.content, src)
        self.assertEqual(res.error["code"], "unsupported_format")

    def test_ill_formed_raises(self):
        with self.assertRaises(PatchError) as ctx:
            apply_json_patch('{"broken":', [{"op": "set_field", "field": "a", "value": 1}])
        self.assertEqual(ctx.exception.code, "invalid_json")


class JsonIndexLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-json-index-")).resolve()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._saved = {
            k: getattr(config, k)
            for k in ("NOTES_ROOT", "INDEX_PATH", "IGNORE_FILE")
        }
        config.NOTES_ROOT = self.vault
        config.INDEX_PATH = self.tmp / "index.db"
        config.IGNORE_FILE = self.tmp / "missing-ignore-file"
        self._saved_embed = core.embed
        self._saved_deferred_dir = deferred.DEFERRED_DIR
        deferred.DEFERRED_DIR = self.tmp / "deferred"
        self._env = {}
        for key in (
            "APO_VAULTS",
            "APO_VAULT_PATHS",
            "APO_VAULTS_ROOT",
            "APO_COLLECTION_ROOT",
        ):
            self._env[key] = os.environ.pop(key, None)
        vaults.invalidate_bindings_cache()

        def fake_embed(texts, **_kwargs):
            out = []
            for text in texts:
                vec = [0.0] * 16
                for token in re.findall(r"\w+", text.lower()):
                    vec[hash(token) % len(vec)] += 1.0
                norm = sum(v * v for v in vec) ** 0.5 or 1.0
                out.append([v / norm for v in vec])
            return out

        core.embed = fake_embed

    def tearDown(self):
        core.writer_close()
        core.reader_close()
        core.embed = self._saved_embed
        deferred.DEFERRED_DIR = self._saved_deferred_dir
        for key, value in self._saved.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        vaults.invalidate_bindings_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, raw: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return path

    def test_object_rebuild_filter_search_update_and_purge(self):
        path = self.write(
            "records/widget.json",
            '{"title":"Widget","status":"open","description":"scarlet turbine","nested":{"owner":"Ada"}}',
        )
        stats = core.index_vault(rebuild=True, verbose=False)
        self.assertEqual(stats.added, 1)
        total, rows = core.filter_notes({"status": "open"}, folder="records")
        self.assertEqual((total, rows[0][1]), (1, "records/widget.json"))
        self.assertEqual(rows[0][2]["nested"]["owner"], "Ada")
        self.assertEqual(core.search("scarlet turbine", folder="records", k=5)[0].path, "records/widget.json")

        path.write_text('{"title":"Widget","status":"closed","description":"violet turbine"}')
        core.index_file(path)
        self.assertEqual(core.filter_notes({"status": "closed"})[0], 1)

        path.unlink()
        core.index_file(path)
        with sqlite3.connect(config.INDEX_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM files WHERE path=?", ("records/widget.json",)).fetchone()[0], 0)

    def test_non_object_and_invalid_json_are_omitted_stably(self):
        array = self.write("records/array.json", "[1,2]\n")
        invalid = self.write("records/broken.json", '{"a":')
        core.index_files([array, invalid], verbose=False)
        with sqlite3.connect(config.INDEX_PATH) as db:
            paths = {row[0] for row in db.execute("SELECT path FROM files")}
        self.assertNotIn("records/array.json", paths)
        self.assertNotIn("records/broken.json", paths)

    def test_object_overwritten_by_array_is_purged_through_queue(self):
        rel = "records/transition.json"
        original = '{"title":"Transition","status":"open","description":"copper signal"}'
        created = ops.write_note(rel, original)
        self.assertTrue(created["ok"], created)
        core.process_queues(config.COLLECTION, scan_vault=False, verbose=False)
        self.assertEqual(core.filter_notes({"status": "open"})[0], 1)
        self.assertEqual(core.search("copper signal", k=5)[0].path, rel)

        raw_array = '[1,  2]\n'
        overwritten = ops.write_note(rel, raw_array)
        self.assertTrue(overwritten["ok"], overwritten)
        self.assertEqual(overwritten["index_status"], "unsupported_json_top_level")
        self.assertEqual((self.vault / rel).read_text(encoding="utf-8"), raw_array)

        queued = deferred.load_index_queue(config.COLLECTION)
        self.assertIn(str((self.vault / rel).resolve()), queued)
        core.process_queues(config.COLLECTION, scan_vault=False, verbose=False)
        self.assertEqual(core.filter_notes({"status": "open"})[0], 0)
        self.assertEqual(core.search("copper signal", k=5), [])

    def test_invalid_overwrite_preserves_object_and_index(self):
        rel = "records/invalid-transition.json"
        original = '{"title":"Stable","status":"open","description":"indigo signal"}'
        created = ops.write_note(rel, original)
        self.assertTrue(created["ok"], created)
        core.process_queues(config.COLLECTION, scan_vault=False, verbose=False)

        invalid = ops.write_note(rel, '{"title":')
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"], "validation_failed")
        self.assertEqual((self.vault / rel).read_text(encoding="utf-8"), original)
        self.assertEqual(deferred.load_index_queue(config.COLLECTION), set())
        self.assertEqual(core.filter_notes({"status": "open"})[0], 1)
        self.assertEqual(core.search("indigo signal", k=5)[0].path, rel)


if __name__ == "__main__":
    unittest.main()
