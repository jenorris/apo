"""Unscoped+exclude must widen FTS without forcing vec0 k=500."""

from __future__ import annotations

import unittest
from unittest import mock

from apo_engine import config, core


class HybridCandidatePoolsTest(unittest.TestCase):
    def test_unscoped_exclude_widens_fts_but_caps_vec(self):
        fts_n, vec_n = core._hybrid_candidate_pools(
            5,
            exclude=True,
            folder_prefix="",
            total_chunks=33516,
        )
        self.assertEqual(fts_n, config.EXCLUDE_CANDIDATE_FLOOR)
        self.assertEqual(vec_n, config.EXCLUDE_VEC_K)
        self.assertLess(vec_n, fts_n)
        self.assertNotEqual(vec_n, 500)

    def test_unscoped_exclude_respects_corpus_size(self):
        fts_n, vec_n = core._hybrid_candidate_pools(
            5,
            exclude=True,
            folder_prefix="",
            total_chunks=40,
        )
        self.assertEqual(fts_n, 40)
        self.assertEqual(vec_n, 40)

    def test_folder_scoped_ignores_exclude_floor(self):
        fts_n, vec_n = core._hybrid_candidate_pools(
            5,
            exclude=True,
            folder_prefix="areas/threads",
            total_chunks=33516,
        )
        base = max(5 * 4, config.SEARCH_CANDIDATES)
        self.assertEqual(fts_n, base)
        self.assertEqual(vec_n, base)

    def test_no_exclude_uses_base_for_both(self):
        fts_n, vec_n = core._hybrid_candidate_pools(
            5,
            exclude=False,
            folder_prefix="",
            total_chunks=33516,
        )
        base = max(5 * 4, config.SEARCH_CANDIDATES)
        self.assertEqual(fts_n, base)
        self.assertEqual(vec_n, base)

    def test_exclude_vec_k_env_override(self):
        with mock.patch.object(config, "EXCLUDE_VEC_K", 48):
            with mock.patch.object(config, "EXCLUDE_CANDIDATE_FLOOR", 500):
                fts_n, vec_n = core._hybrid_candidate_pools(
                    5,
                    exclude=True,
                    folder_prefix="",
                    total_chunks=10000,
                )
        self.assertEqual(fts_n, 500)
        self.assertEqual(vec_n, 48)


if __name__ == "__main__":
    unittest.main()
