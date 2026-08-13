"""YAML catalog notes — index, filter, patch, OKF, search (no Ollama)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apo_engine import config, core, okf, ops
from apo_engine.note_format import is_yaml_note, parse_yaml_document
from apo_engine.yaml_patch import apply_yaml_patch, set_field_path

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


_MINI_CONTRACT = """
okf_version: "0.1"
type_field: okf_type
core_required:
  - okf_type
  - description
  - timestamp
core_soft:
  - title
default_enforcement: soft
default_okf_type: Note
path_rules:
  - match: "records/**/*.yaml"
    enforcement: soft
    okf_type: Fact
"""


class YamlVaultTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-yaml-")).resolve()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._saved = {
            k: getattr(config, k)
            for k in ("NOTES_ROOT", "INDEX_PATH", "MAX_CHARS", "OVERLAP", "IGNORE_FILE")
        }
        config.NOTES_ROOT = self.vault
        config.INDEX_PATH = self.tmp / "index.db"
        config.MAX_CHARS = 200
        config.OVERLAP = 20
        config.IGNORE_FILE = self.tmp / "missing-ignore-file"
        self._saved_embed = core.embed
        core.embed = _fake_embed
        okf.clear_contract_cache()

    def tearDown(self):
        for k, val in self._saved.items():
            setattr(config, k, val)
        core.embed = self._saved_embed
        core.writer_close()
        core.reader_close()
        core._schema_ready.discard(str(config.INDEX_PATH.resolve()))
        okf.clear_contract_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class TestYamlFormatHelpers(unittest.TestCase):
    def test_suffix_helpers(self):
        self.assertTrue(is_yaml_note("a/b.yaml"))
        self.assertTrue(is_yaml_note("a/b.YML"))
        self.assertFalse(is_yaml_note("a/b.md"))

    def test_nested_set_field(self):
        data: dict = {"meta": {"owner": "x"}}
        set_field_path(data, "meta.owner", "jeremy")
        set_field_path(data, "meta.team", "platform")
        self.assertEqual(data["meta"]["owner"], "jeremy")
        self.assertEqual(data["meta"]["team"], "platform")

    def test_list_index_and_id_selector(self):
        data: dict = {
            "todos": [
                {"id": "a", "status": "pending"},
                {"id": "b", "status": "pending"},
            ]
        }
        set_field_path(data, "todos.0.status", "done")
        self.assertEqual(data["todos"][0]["status"], "done")
        set_field_path(data, "todos[id=b].status", "completed")
        self.assertEqual(data["todos"][1]["status"], "completed")

    def test_id_selector_ambiguous(self):
        data: dict = {"todos": [{"id": "x"}, {"id": "x"}]}
        from apo_engine.markdown_patch import PatchError

        with self.assertRaises(PatchError) as ctx:
            set_field_path(data, "todos[id=x].status", "done")
        self.assertEqual(ctx.exception.code, "anchor_ambiguous")


class TestYamlIndexFilterSearch(YamlVaultTestCase):
    def test_yaml_indexed_and_filterable(self):
        self.write(
            "records/alpha.yaml",
            "title: Alpha Queue\nokf_type: Fact\nstatus: open\ndescription: test atom\n",
        )
        self.write("notes/prose.md", "---\ntitle: Prose\n---\n\n# Prose\n\nbody zebra\n")
        core.index_vault(verbose=False)

        total, matches = core.filter_notes({"status": "open"}, folder="records")
        self.assertEqual(total, 1)
        self.assertEqual(matches[0][1], "records/alpha.yaml")
        self.assertEqual(matches[0][2].get("okf_type"), "Fact")

        # Empty mapping still catalogued
        self.write("records/empty.yaml", "{}\n")
        core.index_files([self.vault / "records/empty.yaml"], verbose=False)
        total_empty, empty_hits = core.filter_notes({}, folder="records")
        paths = {p for _, p, _ in empty_hits}
        self.assertIn("records/empty.yaml", paths)

    def test_contract_yaml_ignored_by_default(self):
        cfg = self.vault / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "okf-contract.schema.yaml").write_text("okf_version: '0.1'\n", encoding="utf-8")
        self.write("records/keep.yaml", "title: Keep\nstatus: active\n")
        core.index_vault(verbose=False)
        paths = [
            r[0]
            for r in sqlite3.connect(config.INDEX_PATH).execute("SELECT path FROM files")
        ]
        self.assertIn("records/keep.yaml", paths)
        self.assertNotIn("system/config/okf-contract.schema.yaml", paths)

    def test_search_hits_title_description(self):
        self.write(
            "records/zebra-device.yaml",
            "title: Zebra Device\ndescription: inventory for zebra laptop\nstatus: active\n",
        )
        core.index_vault(verbose=False)
        hits = core.search("zebra laptop", k=5, folder="records")
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "records/zebra-device.yaml")


class TestYamlOps(YamlVaultTestCase):
    def test_write_read_patch_nested(self):
        r = ops.write_note(
            "records/item.yaml",
            "title: Item\nstatus: open\nmeta:\n  owner: a\n",
        )
        self.assertTrue(r["ok"], r)

        read = ops.read_note("records/item.yaml")
        self.assertTrue(read["ok"])
        self.assertEqual(read["frontmatter"]["status"], "open")
        self.assertEqual(read["content"], "")

        bad = ops.read_note("records/item.yaml", heading="Nope")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "unsupported_format")

        append = ops.append_note("records/item.yaml", text="- nope\n")
        self.assertFalse(append["ok"])
        self.assertEqual(append["error"], "unsupported_format")

        patch = ops.patch_note(
            "records/item.yaml",
            [
                {"op": "set_field", "field": "status", "value": "done"},
                {"op": "set_field", "field": "meta.owner", "value": "jeremy"},
            ],
            expected_mtime=r["mtime"],
        )
        self.assertTrue(patch["ok"], patch)
        data = parse_yaml_document((self.vault / "records/item.yaml").read_text())
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["meta"]["owner"], "jeremy")

        reject = apply_yaml_patch(
            "title: x\n",
            [{"op": "append", "text": "nope"}],
        )
        self.assertFalse(reject.ok)
        self.assertEqual(reject.error["code"], "unsupported_format")


class TestYamlOkf(YamlVaultTestCase):
    def setUp(self):
        super().setUp()
        profile = self.vault / "system" / "config" / "okf-contract.schema.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(_MINI_CONTRACT, encoding="utf-8")

    def test_yaml_stamp(self):
        r = okf.process_concept(
            vault_root=self.vault,
            rel_path="records/fact.yaml",
            content="title: Fact One\nstatus: open\n",
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.okf_type, "Fact")
        data = parse_yaml_document(r.content)
        self.assertEqual(data.get("okf_type"), "Fact")
        self.assertIn("description", data)
        self.assertIn("timestamp", data)


if __name__ == "__main__":
    unittest.main()
