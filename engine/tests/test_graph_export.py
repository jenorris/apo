"""OKF contract link_fields + graph_export (node-link JSON)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import cli_ops, config, core, links, ops, ranking, vaults
from apo_engine.okf import contract as okf_contract
from test_retrieval_gaps import _fake_embed

_CONTRACT = "system/contracts/okf-contract.schema.yaml"


class LinkFieldsContractTest(unittest.TestCase):
    def load(self, data: dict) -> okf_contract.OkfContract:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "c.yaml"
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        return okf_contract.load_contract(p)

    def test_absent_means_engine_defaults(self):
        c = self.load({"okf_version": "0.2"})
        self.assertIsNone(c.link_fields)
        self.assertEqual(okf_contract.link_fields_for(c, "a.md"), links.DEFAULT_LINK_FIELDS)
        self.assertEqual(okf_contract.link_fields_for(None, "a.md"), links.DEFAULT_LINK_FIELDS)

    def test_empty_list_opts_out(self):
        c = self.load({"link_fields": []})
        self.assertEqual(okf_contract.link_fields_for(c, "a.md"), ())

    def test_entries_parse_and_bad_ones_drop(self):
        c = self.load(
            {
                "link_fields": [
                    {"key": "depends_on", "kind": "depends-on"},
                    "parents",  # bare key → kind = key
                    {"key": "sources.resource"},  # kind = last segment
                    {"key": "x[]", "kind": "bad"},  # [] not fm_path syntax
                    {"key": "y", "kind": "Not A Slug!"},
                    42,
                ]
            }
        )
        self.assertEqual(
            c.link_fields,
            [("depends_on", "depends-on"), ("parents", "parents"), ("sources.resource", "resource")],
        )
        codes = okf_contract.check_contract(
            {"link_fields": [{"key": "x[]", "kind": "bad"}, {"key": "y", "kind": "Not A Slug!"}, 42]}
        )
        self.assertEqual([f["field"] for f in codes], ["link_fields[0]", "link_fields[1]", "link_fields[2]"])
        self.assertEqual(okf_contract.check_contract({"link_fields": {"key": "x"}})[0]["field"], "link_fields")

    def test_path_rule_extends_top_level(self):
        c = self.load(
            {
                "link_fields": [{"key": "related", "kind": "related"}],
                "path_rules": [
                    {"match": "adr/**", "link_fields": [{"key": "supersedes", "kind": "supersedes"},
                                                        {"key": "related", "kind": "dup"}]},
                ],
            }
        )
        self.assertEqual(okf_contract.link_fields_for(c, "notes/x.md"), (("related", "related"),))
        self.assertEqual(
            okf_contract.link_fields_for(c, "adr/0001.md"),
            (("related", "related"), ("supersedes", "supersedes")),
        )
        self.assertEqual(okf_contract.check_contract({"path_rules": [{"match": "a", "link_fields": "x"}]})[0]["field"],
                         "path_rules[0].link_fields")

    def test_fingerprint_tracks_link_declarations_only(self):
        a = self.load({"link_fields": ["related"], "core_soft": ["title"]})
        b = self.load({"link_fields": ["related"], "core_soft": ["resource"]})
        c = self.load({"link_fields": ["related", "parents"]})
        fp = okf_contract.link_fields_fingerprint
        self.assertEqual(fp(a), fp(b))
        self.assertNotEqual(fp(a), fp(c))
        self.assertNotEqual(fp(None), fp(a))


class GraphExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "graph_export_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        okf_contract.clear_contract_cache()

    def tearDown(self):
        core.writer_close()
        for patch in self._patches:
            patch.stop()
        okf_contract.clear_contract_cache()
        vaults._usage_layout_cache.clear()
        ranking._backlink_count_cache.clear()
        ranking._frontmatter_boost_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def write_contract(self, data: dict, bump: float = 0.0) -> None:
        p = self.write(_CONTRACT, yaml.safe_dump(data))
        if bump:
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + bump))

    def seed(self) -> None:
        self.write("hub.md", "---\ntitle: Hub\nokf_type: Reference\n---\n\n# Hub\n")
        self.write("a/one.md", "---\ntitle: One\nrelated: [../hub.md]\n---\n\n[[two]] [[ghost]]\n")
        self.write("a/two.md", "---\ntitle: Two\n---\n\n[up](../hub.md) [x](https://x.y)\n")
        self.write("b/far.md", "---\ntitle: Far\n---\n\n[[one]]\n")

    def export(self, **kw) -> dict:
        out = ops.graph_export(**kw)
        self.assertTrue(out["ok"], out)
        return out

    def test_contract_kinds_drive_extraction_and_change_triggers_backfill(self):
        self.write_contract({"link_fields": [{"key": "depends_on", "kind": "depends-on"}]})
        self.write("svc.md", "---\ndepends_on: [db.md]\nrelated: [db.md]\n---\n\n# svc\n")
        self.write("db.md", "# db\n")
        core.index_vault(verbose=False)
        kinds = {r["kind"] for r in core.list_links(source="svc.md")}
        self.assertEqual(kinds, {"depends-on"})  # contract replaced the defaults

        self.write_contract({"link_fields": ["related"]}, bump=5)
        self.assertTrue(core.links_backfill_needed(core.writer_connect()))
        core.index_vault(verbose=False)  # no note changed — the fingerprint drives the backfill
        self.assertEqual({r["kind"] for r in core.list_links(source="svc.md")}, {"related"})
        self.assertFalse(core.links_backfill_needed(core.writer_connect()))

    def test_whole_vault_node_link_shape(self):
        self.seed()
        core.index_vault(verbose=False)
        out = self.export()
        self.assertTrue(out["directed"])
        self.assertTrue(out["multigraph"])
        ids = {n["id"] for n in out["nodes"]}
        self.assertEqual(ids, {"hub.md", "a/one.md", "a/two.md", "b/far.md"})
        hub = next(n for n in out["nodes"] if n["id"] == "hub.md")
        self.assertEqual((hub["title"], hub["okf_type"], hub["kind"]), ("Hub", "Reference", "note"))
        pairs = {(e["source"], e["target"], e["kind"]) for e in out["links"]}
        self.assertEqual(
            pairs,
            {
                ("a/one.md", "hub.md", "related"),
                ("a/one.md", "a/two.md", "wikilink"),
                ("a/two.md", "hub.md", "md_link"),
                ("b/far.md", "a/one.md", "wikilink"),
            },
        )
        self.assertTrue(all(e["key"] == e["kind"] and e["status"] == "resolved" for e in out["links"]))
        self.assertEqual(out["node_count"], 4)
        self.assertFalse(out["truncated"])

    def test_include_unresolved_and_kinds(self):
        self.seed()
        core.index_vault(verbose=False)
        out = self.export(include_unresolved=True)
        extra = {n["id"]: n["kind"] for n in out["nodes"] if n["kind"] != "note"}
        self.assertEqual(extra, {"ghost.md": "dangling"})  # body URLs are not links
        only = self.export(kinds=["related"])
        self.assertEqual([(e["source"], e["target"]) for e in only["links"]], [("a/one.md", "hub.md")])
        self.assertEqual(only["graph"]["kinds"], ["related"])

    def test_folder_scope_keeps_both_ends_inside(self):
        self.seed()
        core.index_vault(verbose=False)
        out = self.export(folder="a")
        self.assertEqual({n["id"] for n in out["nodes"]}, {"a/one.md", "a/two.md"})
        self.assertEqual([(e["source"], e["target"]) for e in out["links"]], [("a/one.md", "a/two.md")])

    def test_ego_graph_depth(self):
        self.seed()
        core.index_vault(verbose=False)
        d1 = self.export(path="hub.md", depth=1)
        self.assertEqual(d1["nodes"][0]["id"], "hub.md")
        self.assertEqual({n["id"] for n in d1["nodes"]}, {"hub.md", "a/one.md", "a/two.md"})
        d2 = self.export(path="hub", depth=2)  # .md optional
        self.assertIn("b/far.md", {n["id"] for n in d2["nodes"]})
        self.assertEqual(d2["graph"]["focus"], "hub.md")
        missing = ops.graph_export(path="nope.md")
        self.assertEqual(missing["error"], "not_found")

    def test_max_nodes_truncates_and_drops_dangling_edges(self):
        self.seed()
        core.index_vault(verbose=False)
        out = self.export(path="hub.md", depth=2, limit=2)
        self.assertTrue(out["truncated"])
        ids = {n["id"] for n in out["nodes"]}
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in out["links"]))

    def test_vault_action_and_cli(self):
        self.seed()
        core.index_vault(verbose=False)
        via_vault = ops.vault_op("export", folder="a", max_nodes=None)
        self.assertEqual(via_vault["node_count"], 2)
        dest = self.tmp / "g.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_ops.main(["graph-export", "-o", str(dest)])
        self.assertEqual(rc, 0)
        data = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(data["node_count"], 4)
        self.assertIn("wrote 4 nodes", buf.getvalue())
        bad = ops.vault_op("export", folder="a", path="hub.md")
        self.assertEqual(bad["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
