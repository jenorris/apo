"""Query-side embed prefix (asymmetric BGE) — applied to query only, not passages."""

from __future__ import annotations

import unittest
from unittest import mock

from apo_engine import config, core

_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbedPrefixTest(unittest.TestCase):
    def setUp(self):
        core.clear_query_embed_cache()

    def tearDown(self):
        core.clear_query_embed_cache()

    def test_prefix_applied_to_query_only(self):
        seen: list[str] = []

        def _capture(texts, **kwargs):
            seen.extend(texts)
            return [[0.1] * 8 for _ in texts]

        with mock.patch.object(config, "QUERY_PREFIX", _PREFIX), \
             mock.patch.object(config, "QUERY_EMBED_TTL", 0), \
             mock.patch.object(core, "embed", _capture):
            core.query_embed("pacifica brake work")
        self.assertEqual(seen, [_PREFIX + "pacifica brake work"])

    def test_no_prefix_by_default(self):
        seen: list[str] = []

        def _capture(texts, **kwargs):
            seen.extend(texts)
            return [[0.1] * 8 for _ in texts]

        with mock.patch.object(config, "QUERY_PREFIX", ""), \
             mock.patch.object(config, "QUERY_EMBED_TTL", 0), \
             mock.patch.object(core, "embed", _capture):
            core.query_embed("plain query")
        self.assertEqual(seen, ["plain query"])

    def test_cache_key_is_raw_query(self):
        calls: list[list[str]] = []

        def _capture(texts, **kwargs):
            calls.append(list(texts))
            return [[0.2] * 8 for _ in texts]

        with mock.patch.object(config, "QUERY_PREFIX", _PREFIX), \
             mock.patch.object(config, "QUERY_EMBED_TTL", 120), \
             mock.patch.object(core, "embed", _capture):
            core.query_embed("same query")
            core.query_embed("same query")
        # Second call served from cache — embed invoked once, prefixed once.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], [_PREFIX + "same query"])


if __name__ == "__main__":
    unittest.main()
