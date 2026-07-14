"""Tests for the engine core: chunking, indexing, and hybrid search.

Runs against a temp vault with a deterministic bag-of-words embedder — no Ollama
required. Config is patched on the module (core reads it at call time).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apo_engine import config, core

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    """Deterministic unit-norm bag-of-words vectors: shared tokens ⇒ nearby vectors."""
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


class TestChunkMarkdown(unittest.TestCase):
    def test_real_heading_levels(self):
        text = "# Alpha\n\nalpha body\n\n### Gamma\n\ngamma body\n"
        chunks = core.chunk_markdown(text, max_chars=10, overlap=2)
        by_text = {c.strip(): (head, level) for head, level, c, *_ in chunks}
        self.assertEqual(by_text["alpha body"], ("Alpha", 1))
        # H3 directly under H1: breadcrumb collapses the skipped level, the level must not.
        self.assertEqual(by_text["gamma body"], ("Alpha › Gamma", 3))

    def test_preamble_is_level_zero(self):
        # Greedy packing may merge the preamble with following blocks; the chunk
        # keeps the first block's anchor: no breadcrumb, level 0.
        chunks = core.chunk_markdown("preamble text\n\n# Head\n\nbody\n", max_chars=100, overlap=10)
        head, level, text, *_ = chunks[0]
        self.assertEqual((head, level), ("", 0))
        self.assertTrue(text.startswith("preamble text"))

    def test_frontmatter_stripped(self):
        text = "---\ntitle: T\n---\n\n# Head\n\nbody\n"
        chunks = core.chunk_markdown(text, max_chars=100, overlap=10)
        self.assertNotIn("title: T", "".join(c for _, _, c, *_ in chunks))

    def test_oversized_block_splits_with_shared_anchor(self):
        body = "x" * 250
        chunks = core.chunk_markdown(f"## Big\n\n{body}\n", max_chars=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        for head, level, ctext, *_ in chunks:
            self.assertEqual((head, level), ("Big", 2))
            self.assertLessEqual(len(ctext), 100)

    def test_line_spans_skip_frontmatter(self):
        text = "---\ntitle: T\n---\n\n# Head\n\nbody line\n"
        chunks = core.chunk_markdown(text, max_chars=200, overlap=10)
        self.assertTrue(chunks)
        _h, _l, ctext, start, end = next(c for c in chunks if "body line" in c[2])
        # Frontmatter lines 1-3; blank 4; heading 5; blank 6; body 7
        self.assertEqual(start, 7)
        self.assertGreaterEqual(end, start)


class VaultTestCase(unittest.TestCase):
    """Temp vault + temp index with patched config and embedder."""

    def setUp(self):
        # resolve() matches production config (_path) — macOS tempdirs live behind
        # the /var → /private/var symlink and index_file compares resolved paths.
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-test-")).resolve()
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

    def tearDown(self):
        for k, val in self._saved.items():
            setattr(config, k, val)
        core.embed = self._saved_embed
        core.writer_close()
        core.reader_close()
        core._schema_ready.discard(str(config.INDEX_PATH.resolve()))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def chunk_paths(self) -> list[str]:
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            return [r[0] for r in db.execute("SELECT DISTINCT path FROM chunks")]
        finally:
            db.close()


class TestIndexAndSearch(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.write("projects/zoo.md", "# Zoo\n\nthe zebra sleeps in the shade\n")
        self.write("project-other/finance.md", "# Finance\n\nquarterly finance report totals\n")
        core.index_vault(verbose=False)

    def test_search_finds_relevant_note(self):
        hits = core.search("zebra", k=2)
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "projects/zoo.md")
        self.assertTrue(hits[0].chunk_hash)

    def test_scores_monotonic_with_ranking(self):
        hits = core.search("zebra finance", k=10)
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[0].score, 1.0)
        for a, b in zip(hits, hits[1:]):
            self.assertGreaterEqual(a.score, b.score)

    def test_folder_filter_is_boundary_aware(self):
        self.assertEqual(core.search("finance report", k=10, folder="proj"), [])
        hits = core.search("zebra", k=10, folder="projects")
        self.assertEqual({h.path for h in hits}, {"projects/zoo.md"})

    def test_exclude_globs(self):
        hits = core.search("zebra finance", k=10, exclude=["projects/*"])
        self.assertNotIn("projects/zoo.md", {h.path for h in hits})

    def test_vault_root_indexignore(self):
        self.write(".indexignore", "private/*\n")
        self.write("private/secret.md", "# Secret\n\nhidden zebra fact\n")
        core.index_vault(verbose=False)
        self.assertNotIn("private/secret.md", self.chunk_paths())


class TestIndexLifecycle(VaultTestCase):
    def test_incremental_skips_unchanged_and_prunes_deleted(self):
        note = self.write("a.md", "# A\n\nalpha content\n")
        self.write("b.md", "# B\n\nbeta content\n")
        core.index_vault(verbose=False)

        stats = core.index_vault(verbose=False)
        self.assertEqual((stats.added, stats.changed, stats.chunks), (0, 0, 0))

        note.unlink()
        stats = core.index_vault(verbose=False)
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.chunk_paths(), ["b.md"])

    def test_index_file_replaces_chunks(self):
        note = self.write("n.md", "# N\n\noriginal wombat text\n")
        core.index_vault(verbose=False)
        note.write_text("# N\n\nupdated aardvark text\n", encoding="utf-8")
        core.index_file(note)
        hits = core.search("aardvark", k=3)
        self.assertEqual(hits[0].path, "n.md")
        self.assertNotIn("wombat", " ".join(h.text for h in core.search("wombat", k=3)))

    def test_index_file_purge_of_deleted_note_persists(self):
        note = self.write("gone.md", "# Gone\n\nephemeral content\n")
        core.index_vault(verbose=False)
        note.unlink()
        self.assertEqual(core.index_file(note), 0)
        # Fresh connection: the purge must have been committed, not rolled back on GC.
        self.assertNotIn("gone.md", self.chunk_paths())
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            n = db.execute("SELECT COUNT(*) FROM files WHERE path='gone.md'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(n, 0)

    def test_rebuild_clears_backlinks(self):
        self.write(
            "src.md",
            "---\ntitle: Src\n---\n\nSee [[Target Note]] and [[other]].\n",
        )
        core.index_vault(verbose=False)
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            n1 = db.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0]
        finally:
            db.close()
        self.assertGreaterEqual(n1, 2)
        core.index_vault(rebuild=True, verbose=False)
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            n2 = db.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(n1, n2)

    def test_filter_notes_equality_and_contains(self):
        self.write("a.md", "---\nstatus: active\ntags: [x]\n---\n\n# A\n\nbody a\n")
        self.write("b.md", "---\nstatus: done\ntags: [y]\n---\n\n# B\n\nbody b\n")
        core.index_vault(verbose=False)
        total, rows = core.filter_notes({"status": "active"})
        self.assertEqual(total, 1)
        self.assertEqual(rows[0][1], "a.md")
        total, rows = core.filter_notes({"tags": {"$contains": "y"}})
        self.assertEqual(total, 1)
        self.assertEqual(rows[0][1], "b.md")

    def test_filter_notes_sql_limit_pages(self):
        for i in range(5):
            self.write(
                f"n{i}.md",
                f"---\nstatus: active\nseq: {i}\n---\n\n# N{i}\n\nbody {i}\n",
            )
        core.index_vault(verbose=False)
        total, rows = core.filter_notes({"status": "active"}, limit=2)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows), 2)

    def test_recent_preview_and_frontmatter_field(self):
        self.write("t.md", "---\ntitle: Hello\n---\n\n# Head\n\npreview body here\n")
        core.index_vault(verbose=False)
        rows = core.recent_notes_preview(limit=5)
        self.assertTrue(rows)
        path, _mt, preview = rows[0]
        self.assertEqual(path, "t.md")
        self.assertIn("preview", preview.lower())
        self.assertEqual(core.frontmatter_field("t.md", "title"), "Hello")

    def test_snippet_chars_and_exclude_compile(self):
        self.write("projects/a.md", "# A\n\nzebra alpha long body text for snippet\n")
        self.write("personal/b.md", "# B\n\nzebra beta\n")
        core.index_vault(verbose=False)
        full = core.search("zebra", k=5, hybrid=False)
        self.assertTrue(full)
        snip = core.search("zebra", k=5, hybrid=False, snippet_chars=12)
        self.assertTrue(all(len(h.text) <= 12 for h in snip))
        prefs, globs = core._compile_excludes(["projects/*", "**/secret*.md"])
        self.assertEqual(prefs, ["projects/"])
        self.assertTrue(globs)
        self.assertTrue(core._path_excluded("projects/a.md", prefs, globs))
        self.assertFalse(core._path_excluded("personal/b.md", prefs, globs))

    def test_iter_notes_prunes_obsidian(self):
        obs = self.vault / ".obsidian" / "plugins"
        obs.mkdir(parents=True)
        (obs / "noise.md").write_text("# noise\n", encoding="utf-8")
        self.write("ok.md", "# Ok\n\nvisible\n")
        paths = list(core._iter_notes(self.vault, core._load_ignore()))
        rels = {p.relative_to(self.vault).as_posix() for p in paths}
        self.assertIn("ok.md", rels)
        self.assertNotIn(".obsidian/plugins/noise.md", rels)

    def test_blake2_hashes_and_migration_without_reembed(self):
        sample = "hello vault\n"
        self.assertEqual(len(core._file_hash(sample)), 64)
        self.assertEqual(len(core._content_hash(sample)), 16)
        self.assertNotEqual(
            core._file_hash(sample),
            hashlib.sha256(sample.encode()).hexdigest(),
        )

        self.write("keep.md", "---\ntitle: Keep\n---\n\n# Keep\n\nstable body zebra\n")
        core.index_vault(verbose=False)
        # Simulate a pre-blake2 index: sha256 digests + stale meta.
        db = core.writer_connect(ensure_hash=False)
        text = (self.vault / "keep.md").read_text(encoding="utf-8")
        db.execute(
            "UPDATE files SET hash=?",
            (hashlib.sha256(text.encode()).hexdigest(),),
        )
        for rid, ctext in db.execute("SELECT id, text FROM chunks"):
            db.execute(
                "UPDATE chunks SET content_hash=? WHERE id=?",
                (hashlib.sha256((ctext or "").encode()).hexdigest()[:16], rid),
            )
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('hash_algo', 'sha256')"
        )
        db.commit()
        core.writer_close()

        embed_calls = {"n": 0}
        real_embed = core.embed

        def counting(texts, **kwargs):
            embed_calls["n"] += len(texts)
            return real_embed(texts, **kwargs)

        core.embed = counting
        try:
            core.index_vault(verbose=False)
        finally:
            core.embed = real_embed
        self.assertEqual(embed_calls["n"], 0)
        db = core.reader_connect()
        algo = db.execute("SELECT value FROM meta WHERE key='hash_algo'").fetchone()[0]
        self.assertEqual(algo, core.HASH_ALGO)
        stored = db.execute("SELECT hash FROM files WHERE path='keep.md'").fetchone()[0]
        self.assertEqual(stored, core._file_hash(text))
        ch = db.execute("SELECT content_hash, text FROM chunks LIMIT 1").fetchone()
        self.assertEqual(ch[0], core._content_hash(ch[1]))


if __name__ == "__main__":
    unittest.main()
