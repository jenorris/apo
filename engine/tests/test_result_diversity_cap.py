"""Regression test for ``core.search``'s post-fusion result-diversity cap
(``config.RESULT_DIVERSITY_CAP`` / ``ranking.diversity_group_key``).

Every existing post-fusion boost (phrase/stem, slug/ticket, title/frontmatter,
backlink, neighbor-rank, catalog) operates per-(path, query) and applies
identically to every chunk of that path — there was no per-path cap anywhere in
the fused result list, so a single crowded table (many rows all matching the
same query term) could consume most or all of top-k at the expense of every
other matching note.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core

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


class ResultDiversityCapTest(unittest.TestCase):
    """A 6-row table that would otherwise dominate top-k on a shared query term
    must not crowd out every other matching note."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "tables").mkdir(parents=True)
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "tables" / "big-table.md").write_text(
            "---\ntitle: Big Table\n---\n\n"
            "# Big Table\n\n"
            "| Widget | Detail |\n"
            "|--------|--------|\n"
            "| Alpha  | first  |\n"
            "| Beta   | second |\n"
            "| Gamma  | third  |\n"
            "| Delta  | fourth |\n"
            "| Epsilon| fifth  |\n"
            "| Zeta   | sixth  |\n",
            encoding="utf-8",
        )
        for name in ("alpha", "beta", "gamma"):
            (self.vault / "notes" / f"{name}-report.md").write_text(
                f"---\ntitle: {name.title()} Report\n---\n\n"
                f"# {name.title()} Report\n\n"
                f"The widget shipment arrived on schedule this quarter for the {name} team.\n",
                encoding="utf-8",
            )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "diversity_cap_test"),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for patch in self._patches:
            patch.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        core.writer_close()
        core.reader_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crowded_table_does_not_dominate_top_k(self):
        hits = core.search("widget", k=5, hybrid=True)
        paths = [h.path for h in hits]
        self.assertEqual(len(hits), 5, paths)

        big_table_count = sum(1 for p in paths if p == "tables/big-table.md")
        self.assertLessEqual(big_table_count, config.RESULT_DIVERSITY_CAP, paths)

        distinct_paths = len(set(paths))
        self.assertGreaterEqual(distinct_paths, 4, paths)
        for name in ("alpha", "beta", "gamma"):
            self.assertIn(f"notes/{name}-report.md", paths)


if __name__ == "__main__":
    unittest.main()
