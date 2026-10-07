"""llamacpp embed backend: wire format, passage/query prefixes, Matryoshka truncation."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer

from apo_engine import config, core

from test_core import VaultTestCase

_NATIVE_DIM = 8


def _vec(text: str) -> list[float]:
    """Deterministic, un-normalized vector so truncation + renorm is observable."""
    base = float(len(text) % 7 + 1)
    return [base + i for i in range(_NATIVE_DIM)]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, body))
        inputs = body["input"]
        if any("poison" in t for t in inputs):
            self.send_response(500)
            self.end_headers()
            return
        # Return rows reversed to prove the client orders by "index".
        data = [{"index": i, "embedding": _vec(t)} for i, t in enumerate(inputs)][::-1]
        out = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class LlamaCppCase(VaultTestCase):
    def setUp(self):
        super().setUp()
        core.embed = self._saved_embed  # exercise the real embed()
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._cfg = {
            k: getattr(config, k)
            for k in ("EMBED_BACKEND", "LLAMACPP_URL", "DOC_PREFIX", "QUERY_PREFIX", "EMBED_DIM", "MODEL_NAME")
        }
        config.EMBED_BACKEND = "llamacpp"
        config.LLAMACPP_URL = f"http://127.0.0.1:{self.server.server_port}"
        config.DOC_PREFIX = ""
        config.QUERY_PREFIX = ""
        config.EMBED_DIM = 0
        config.MODEL_NAME = "eg-test"
        core.clear_query_embed_cache()

    def tearDown(self):
        for k, v in self._cfg.items():
            setattr(config, k, v)
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def sent_inputs(self) -> list[str]:
        return [t for _p, b in self.server.seen for t in b["input"]]


class TestWireAndPrefixes(LlamaCppCase):
    def test_posts_openai_shape_and_restores_input_order(self):
        out = core.embed(["a", "bbbb", "cc"])
        path, body = self.server.seen[0]
        self.assertEqual(path, "/v1/embeddings")
        self.assertEqual(body["input"], ["a", "bbbb", "cc"])
        self.assertEqual(body["model"], "eg-test")
        self.assertEqual(out, [_vec("a"), _vec("bbbb"), _vec("cc")])

    def test_doc_prefix_on_passages_only(self):
        config.DOC_PREFIX = "title: none | text: "
        config.QUERY_PREFIX = "task: search result | query: "
        core.embed(["a passage"])
        core.query_embed("a question")
        self.assertEqual(
            self.sent_inputs(),
            ["title: none | text: a passage", "task: search result | query: a question"],
        )

    def test_no_prefix_configured_sends_text_unchanged(self):
        core.embed(["plain"])
        core.query_embed("plain q")
        self.assertEqual(self.sent_inputs(), ["plain", "plain q"])

    def test_degraded_retry_gets_the_passage_prefix_once(self):
        config.DOC_PREFIX = "P: "
        core.embed(["text"])
        self.assertEqual(self.sent_inputs(), ["P: text"])


class TestMatryoshka(LlamaCppCase):
    def test_truncates_and_renormalizes(self):
        config.EMBED_DIM = 4
        (v,) = core.embed(["abc"])
        self.assertEqual(len(v), 4)
        self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=6)
        raw = _vec("abc")[:4]
        norm = sum(x * x for x in raw) ** 0.5
        self.assertAlmostEqual(v[0], raw[0] / norm, places=6)

    def test_dim_zero_or_larger_than_native_leaves_vector_alone(self):
        for dim in (0, _NATIVE_DIM, 999):
            config.EMBED_DIM = dim
            self.assertEqual(core.embed(["abc"])[0], _vec("abc"))

    def test_query_vectors_are_truncated_too(self):
        config.EMBED_DIM = 4
        self.assertEqual(len(core.query_embed("some question")), 4)

    def test_index_dimension_follows_embed_dim(self):
        config.EMBED_DIM = 4
        self.write("n.md", "# N\n\nsome body text\n")
        core.index_vault(verbose=False)
        db = sqlite3.connect(config.INDEX_PATH)
        try:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()[0], "4")
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='backend'").fetchone()[0], "llamacpp")
        finally:
            db.close()


class TestFailureIsolation(LlamaCppCase):
    def test_poisoned_input_is_isolated_by_bisection(self):
        out = core.embed(["good one", "poison pill", "good two"])
        self.assertIsNotNone(out[0])
        self.assertIsNone(out[1])
        self.assertIsNotNone(out[2])

    def test_server_down_returns_none_for_every_input(self):
        self.server.shutdown()
        self.server.server_close()
        out = core.embed(["a", "b"])
        self.assertEqual(out, [None, None])
        # restart so tearDown's shutdown() is safe
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    unittest.main()
