"""Typed links: extraction, set-based resolution, migration/backfill, graph queries."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, links, ops, ranking, vaults
from test_retrieval_gaps import _fake_embed


def _by_key(rows: list[links.Link]) -> dict[str, links.Link]:
    return {r.target_key: r for r in rows}


class ExtractLinksTest(unittest.TestCase):
    def extract(self, text: str, fm: dict | None = None, rel: str = "docs/ref/n.md", **kw):
        return links.extract_links(rel, text, fm, **kw)

    def test_wikilink_alias_heading_and_self_anchor(self):
        rows = self.extract("See [[Foo|the foo]], [[bar/Baz#H]] and [[#local]].\n")
        got = _by_key(rows)
        self.assertEqual(set(got), {"foo", "bar/baz"})
        self.assertEqual(got["foo"].alias, "the foo")
        self.assertEqual(got["bar/baz"].heading, "H")
        self.assertEqual(got["foo"].mode, "name")
        self.assertEqual(got["foo"].line, 1)

    def test_media_embeds_are_not_links(self):
        self.assertEqual(self.extract("![[diagram.png]] ![[spec.pdf]]\n"), [])

    def test_vault_prefix_is_foreign_but_colon_titles_are_names(self):
        rows = self.extract("[[work:Other Note]] and [[Meeting: notes]]\n")
        got = _by_key(rows)
        self.assertEqual(got["other note"].status, "foreign")
        self.assertEqual(got["other note"].target_vault, "work")
        self.assertEqual(got["meeting: notes"].status, "unresolved")

    def test_markdown_links_resolve_relative_to_source(self):
        text = (
            "[a](./sib.md#sec) [b](../top.md) [c](<my note.md>) [d](my%20note.md) "
            "[e](/root.md) [f](../../../out.md)\n"
        )
        got = _by_key(self.extract(text))
        self.assertEqual(got["docs/ref/sib"].heading, "sec")
        self.assertIn("docs/top", got)
        self.assertIn("docs/ref/my note", got)
        self.assertIn("root", got)
        self.assertEqual(got["../out.md"].status, "external")
        self.assertTrue(all(r.kind == "md_link" and r.mode == "path" for r in got.values()))

    def test_body_skips_urls_images_anchors_and_non_notes(self):
        text = "[u](https://x.y) ![i](a.md) [h](#only) [p](file.pdf) [m](mailto:a@b.c)\n"
        self.assertEqual(self.extract(text), [])

    def test_code_is_not_scanned(self):
        text = "`[[inline]]` text\n```\n[[fenced]]\n[x](f.md)\n```\n~~~~\n[[tilde]]\n~~~~\n[[real]]\n"
        self.assertEqual([r.target_key for r in self.extract(text)], ["real"])

    def test_frontmatter_link_fields(self):
        text = (
            "---\ntitle: N\nrelated:\n  - ../tier-1.md\n  - \"[[Hub Note]]\"\n  - plain-name\n"
            "sources:\n  - {id: a, resource: https://ex.com/a}\n  - {id: b, resource: ./local.md}\n"
            "supersedes: 2026-01-01\n---\n\nbody [[Hub Note]]\n"
        )
        fm = {
            "title": "N",
            "related": ["../tier-1.md", "[[Hub Note]]", "plain-name"],
            "sources": [{"id": "a", "resource": "https://ex.com/a"}, {"id": "b", "resource": "./local.md"}],
            "supersedes": dt.date(2026, 1, 1),
        }
        rows = self.extract(text, fm)
        by_kind = {(r.kind, r.target_key): r for r in rows}
        self.assertEqual(by_kind[("related", "docs/tier-1")].mode, "path")
        self.assertEqual(by_kind[("related", "docs/tier-1")].line, 3)
        self.assertEqual(by_kind[("related", "hub note")].mode, "name")
        self.assertIn(("related", "plain-name"), by_kind)
        self.assertEqual(by_kind[("source", "https://ex.com/a")].status, "external")
        self.assertIn(("source", "docs/ref/local"), by_kind)
        self.assertEqual(by_kind[("source", "docs/ref/local")].line, 7)
        # Body wikilink counted once; the frontmatter [[Hub Note]] is a `related` edge.
        self.assertEqual([r.kind for r in rows if r.target_key == "hub note"], ["wikilink", "related"])
        self.assertFalse(any(r.kind == "supersedes" for r in rows))  # dates are not links

    def test_yaml_note_uses_fields_only(self):
        rows = links.extract_links(
            "data/x.yaml", "related: [[a]]\nnote: \"[[b]]\"\n", {"related": "a"}, body=False
        )
        self.assertEqual([(r.kind, r.target_key) for r in rows], [("related", "a")])

    def test_link_key_is_ascii_lowercase(self):
        self.assertEqual(links.link_key("Docs/Ünï.md"), "docs/Ünï")


class IndexedLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "links_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        core.writer_close()
        for patch in self._patches:
            patch.stop()
        vaults._usage_layout_cache.clear()
        ranking._backlink_count_cache.clear()
        ranking._frontmatter_boost_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def rows(self, source: str) -> list[dict]:
        return core.list_links(source=source)

    def backlink_sources(self, rel: str) -> set[str]:
        return {b["path"] for b in ops.backlinks(rel)["backlinks"]}

    def test_typed_rows_and_statuses(self):
        self.write("a/target.md", "---\ntitle: Target Title\n---\n\n# T\n")
        self.write(
            "a/src.md",
            "---\ntitle: Src\nrelated:\n  - ./target.md\n  - ../nowhere.md\n---\n\n"
            "[[target]] [[Target Title|by title]] [md](target.md) [[ghost]]\n",
        )
        core.index_vault(verbose=False)
        got = {(r["kind"], r["target_key"]): r for r in self.rows("a/src.md")}
        for key in [("related", "a/target"), ("wikilink", "target"), ("wikilink", "target title"),
                    ("md_link", "a/target")]:
            self.assertEqual(got[key]["status"], "resolved", key)
            self.assertEqual(got[key]["target_path"], "a/target.md", key)
        self.assertEqual(got[("related", "nowhere")]["status"], "dangling")
        self.assertEqual(got[("wikilink", "ghost")]["status"], "dangling")
        self.assertEqual(self.backlink_sources("a/target.md"), {"a/src.md"})

    def test_late_target_resolves_and_delete_dangles(self):
        src = self.write("src.md", "See [[later]].\n")
        core.index_files([src])
        self.assertEqual(self.rows("src.md")[0]["status"], "dangling")
        later = self.write("notes/later.md", "# Later\n")
        core.index_files([later])
        row = self.rows("src.md")[0]
        self.assertEqual((row["status"], row["target_path"]), ("resolved", "notes/later.md"))
        later.unlink()
        core.purge_source(later)
        row = self.rows("src.md")[0]
        self.assertEqual((row["status"], row["target_path"]), ("dangling", None))

    def test_stem_ambiguity_tracks_add_and_remove(self):
        a = self.write("a/foo.md", "# A\n")
        src = self.write("src.md", "[[foo]]\n")
        core.index_files([a, src])
        self.assertEqual(self.rows("src.md")[0]["target_path"], "a/foo.md")
        b = self.write("b/foo.md", "# B\n")
        core.index_files([b])
        self.assertEqual(self.rows("src.md")[0]["status"], "ambiguous")
        self.assertEqual(self.backlink_sources("a/foo.md"), set())
        self.assertEqual(self.backlink_sources("b/foo.md"), set())
        b.unlink()
        core.index_files([b])  # missing path → purge
        self.assertEqual(self.rows("src.md")[0]["target_path"], "a/foo.md")
        self.assertEqual(self.backlink_sources("a/foo.md"), {"src.md"})

    def test_move_redirects_inbound_links(self):
        old = self.write("x/note.md", "---\ntitle: Moved Note\n---\n# N\n")
        src = self.write("src.md", "[[Moved Note]] and [p](x/note.md)\n")
        core.index_files([old, src])
        new = self.vault / "y" / "note.md"
        new.parent.mkdir()
        old.rename(new)
        core.index_files([old, new])
        got = {r["kind"]: r for r in self.rows("src.md")}
        self.assertEqual(got["wikilink"]["target_path"], "y/note.md")  # by title
        self.assertEqual(got["md_link"]["status"], "dangling")  # path link really broke

    def test_graph_neighbors_kinds_filter(self):
        self.write("hub.md", "# Hub\n")
        self.write("leaf.md", "---\nrelated:\n  - hub.md\n---\n\n[[hub]]\n")
        core.index_vault(verbose=False)
        both = ops.graph_neighbors("hub.md", direction="in")
        self.assertEqual({e["kind"] for e in both["edges"]}, {"related", "wikilink"})
        only = ops.graph_neighbors("hub.md", direction="in", kinds=["related"])
        self.assertEqual([(e["from"], e["kind"], e["status"]) for e in only["edges"]],
                         [("leaf.md", "related", "resolved")])
        out = ops.graph_neighbors("leaf.md", direction="out", kinds=["wikilink"])
        self.assertEqual([e["to"] for e in out["edges"]], ["hub.md"])

    def _downgrade_to_legacy_schema(self) -> None:
        """Rewrite the index to the pre-typed-link schema (5-column backlinks, no link_*)."""
        core.writer_close()
        db = sqlite3.connect(self.index)
        try:
            db.executescript(
                """
                CREATE TABLE backlinks_old AS
                    SELECT source, target_key, target_stem, line, text FROM backlinks;
                DROP TABLE backlinks;
                ALTER TABLE backlinks_old RENAME TO backlinks;
                DROP INDEX files_link_key;
                DROP INDEX files_link_stem;
                DROP INDEX files_link_title;
                ALTER TABLE files DROP COLUMN link_title;
                ALTER TABLE files DROP COLUMN link_stem;
                ALTER TABLE files DROP COLUMN link_key;
                DELETE FROM meta WHERE key = 'links_schema_version';
                """
            )
            db.commit()
        finally:
            db.close()
        core._schema_ready.clear()

    def test_migration_backfills_and_rebuild_still_works(self):
        self.write("t.md", "# T\n")
        self.write("s.md", "---\nrelated: [t.md]\n---\n\n[[t]]\n")
        core.index_vault(verbose=False)
        self._downgrade_to_legacy_schema()

        # Reader on the un-migrated index: legacy key matching, no OperationalError.
        legacy = core.list_links(target_keys={"t"})
        self.assertEqual({(r["source"], r["status"]) for r in legacy}, {("s.md", "unresolved")})
        self.assertEqual(self.backlink_sources("t.md"), {"s.md"})

        core.index_vault(verbose=False)  # writer migrates, then backfills (no file changed)
        got = {r["kind"]: r for r in self.rows("s.md")}
        self.assertEqual(set(got), {"wikilink", "related"})
        self.assertTrue(all(r["target_path"] == "t.md" for r in got.values()))
        self.assertFalse(core.links_backfill_needed(core.writer_connect()))

        before = len(self.rows("s.md"))
        core.index_vault(rebuild=True, verbose=False)
        self.assertEqual(len(self.rows("s.md")), before)
        self.assertFalse(core.links_backfill_needed(core.writer_connect()))

    def test_backlink_boost_counts_resolved_sources_only(self):
        self.write("a/foo.md", "# A foo\n")
        self.write("b/foo.md", "# B foo\n")
        self.write("one.md", "[[a/foo]]\n")
        self.write("two.md", "[[foo]]\n")  # ambiguous — must not inflate either target
        core.index_vault(verbose=False)
        ranking._prefetch_path_boost_data(["a/foo.md", "b/foo.md"])
        self.assertEqual(ranking._backlink_count("a/foo.md"), 1)
        self.assertEqual(ranking._backlink_count("b/foo.md"), 0)


if __name__ == "__main__":
    unittest.main()
