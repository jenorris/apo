"""search_expanded()'s fusion refinements — top-rank bonus and position-aware rerank
blending, adapted from qmd's architecture doc (a deeper pass than its top-level
README, which just documents plain RRF for the MCP `query` tool).

Isolates the fusion math from the index/embed machinery by monkeypatching
search_lex_only / search_vector_only / rerank.rerank_scores directly. Assertions are
known-answer: expected values are computed from the documented formula rather than
asserted as "X beats Y" narratives — the bonus/blend are deliberately small
corrections, not rank overrides, so which chunk "wins" is sensitive to the exact
magnitudes chosen and not a robust thing to assert on.
"""

from __future__ import annotations

import unittest
from unittest import mock

from apo_engine import config, core, rerank


def _hit(chunk_hash: str, text: str = "body") -> core.Hit:
    return core.Hit(path=f"{chunk_hash}.md", heading="H", text=text, score=0.0, chunk_hash=chunk_hash)


class TopRankBonusTest(unittest.TestCase):
    def test_bonus_matches_documented_formula_by_rank_tier(self):
        # One sub-query list: plain RRF is 1/(RRF_K+rank) per hit. Verify the
        # +0.05*top (rank 0) / +0.02*top (rank 1-2) / +0 (rank 3+) bonus lands
        # exactly as documented, then renormalizes against the boosted top.
        hits = [_hit("r0"), _hit("r1"), _hit("r2"), _hit("r3")]
        with (
            mock.patch.object(core, "search_lex_only", return_value=hits),
            mock.patch.object(config, "RERANK", False),
        ):
            out = core.search_expanded(
                queries=[{"type": "lex", "query": "q", "weight": 1.0}], k=10
            )
        by_hash = {h.chunk_hash: h.score for h in out}

        k_ = core.RRF_K
        raw = {f"r{i}": 1.0 / (k_ + i) for i in range(4)}
        top_raw = raw["r0"]
        boosted = {
            "r0": raw["r0"] + 0.05 * top_raw,
            "r1": raw["r1"] + 0.02 * top_raw,
            "r2": raw["r2"] + 0.02 * top_raw,
            "r3": raw["r3"],
        }
        boosted_top = max(boosted.values())
        for chash, raw_boosted in boosted.items():
            self.assertAlmostEqual(by_hash[chash], round(raw_boosted / boosted_top, 4), places=4)

        # Rank order within one list is unaffected — the bonus scales with the same
        # ordering RRF already produced, never promotes a worse rank over a better one.
        self.assertEqual([h.chunk_hash for h in out], ["r0", "r1", "r2", "r3"])

    def test_bonus_is_not_a_no_op(self):
        # Sanity check the fixture actually exercises the bonus branch: r1's score
        # must differ from what plain (unboosted) RRF alone would have produced.
        hits = [_hit("r0"), _hit("r1")]
        with (
            mock.patch.object(core, "search_lex_only", return_value=hits),
            mock.patch.object(config, "RERANK", False),
        ):
            out = core.search_expanded(
                queries=[{"type": "lex", "query": "q", "weight": 1.0}], k=10
            )
        by_hash = {h.chunk_hash: h.score for h in out}
        k_ = core.RRF_K
        plain_r1_normalized = round((1.0 / (k_ + 1)) / (1.0 / k_), 4)
        self.assertNotAlmostEqual(by_hash["r1"], plain_r1_normalized, places=4)


class PositionAwareRerankBlendTest(unittest.TestCase):
    def test_blend_matches_documented_weight_by_rrf_rank_tier(self):
        # 12 hits from one lex list -> plain RRF rank tiers 0-2 (w=.75), 3-9 (w=.60),
        # 10-11 (w=.40). Reranker scores set to the reverse rank order so the blend's
        # effect is unambiguous (not swamped by retrieval agreeing anyway).
        hits = [_hit(f"h{i}") for i in range(12)]
        rerank_scores = [float(11 - i) for i in range(12)]  # h0 worst, h11 best
        with (
            mock.patch.object(core, "search_lex_only", return_value=hits),
            mock.patch.object(config, "RERANK", True),
            mock.patch.object(config, "RERANK_POOL", 12),
            mock.patch.object(
                rerank, "rerank_scores", return_value=(rerank_scores, {"applied": True, "detail": "x"})
            ),
        ):
            out = core.search_expanded(
                queries=[{"type": "lex", "query": "q", "weight": 1.0}], k=12
            )
        by_hash = {h.chunk_hash: h.score for h in out}

        # Reconstruct the exact pre-rerank (bonus-boosted, normalized) retrieval
        # score per hit — same formula test_bonus_matches_documented_formula_by_rank_tier
        # already verified independently.
        k_ = core.RRF_K
        raw = {i: 1.0 / (k_ + i) for i in range(12)}
        top_raw = raw[0]
        boosted = {i: raw[i] + (0.05 if i == 0 else 0.02 if i <= 2 else 0.0) * top_raw for i in range(12)}
        boosted_top = max(boosted.values())
        # search_expanded() rounds the retrieval score to 4 places (the value it
        # sets on Hit.score) BEFORE the blend consumes it — match that here.
        retrieval = {i: round(boosted[i] / boosted_top, 4) for i in range(12)}

        lo, hi = min(rerank_scores), max(rerank_scores)
        span = hi - lo
        for i in range(12):
            rr_norm = (rerank_scores[i] - lo) / span
            w = 0.75 if i < 3 else 0.60 if i < 10 else 0.40
            expected = round(w * retrieval[i] + (1 - w) * rr_norm, 4)
            self.assertAlmostEqual(by_hash[f"h{i}"], expected, places=4, msg=f"h{i} (rrf_rank {i})")

    def test_rerank_unavailable_falls_back_to_fused_order(self):
        hits = [_hit("a"), _hit("b")]
        with (
            mock.patch.object(core, "search_lex_only", return_value=hits),
            mock.patch.object(config, "RERANK", True),
            mock.patch.object(
                rerank, "rerank_scores", return_value=(None, {"applied": False, "detail": "unavailable"})
            ),
        ):
            out = core.search_expanded(
                queries=[{"type": "lex", "query": "q", "weight": 1.0}], k=10
            )
        self.assertEqual([h.chunk_hash for h in out], ["a", "b"])
        self.assertEqual(core.last_search_rerank(), {"applied": False, "detail": "unavailable"})


if __name__ == "__main__":
    unittest.main()
