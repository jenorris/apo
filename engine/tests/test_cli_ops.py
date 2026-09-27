"""``apo-local`` (``cli_ops.main``) — happy paths + a validation-rejection case.

No prior test imported ``cli_ops`` at all: everything the CLI does (argparse
wiring, JSON loading, pydantic validation, diff printing, exit codes) was
exercised only by hand. Uses the same legacy single-vault config-patch +
fake-embedder fixture as ``test_ops_chunk_hash_anchor.py`` (``config.NOTES_ROOT``
etc., ``VAULTS_CONFIG=""`` so ``vaults.load_bindings()`` falls back to a
"default" binding) rather than a usage-contract vault dir — cli_ops routes
into the same ``ops.py`` either way.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import cli_ops, config, core, ops

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


class CliOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "a.md").write_text(
            "---\nfoo: 1\n---\n\n# A\n\nhello world\n", encoding="utf-8"
        )
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "cli_ops_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_ops.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _run_json(self, argv: list[str]) -> tuple[int, dict, str]:
        rc, out, err = self._run(argv)
        return rc, json.loads(out), err

    # ---- happy paths -----------------------------------------------------

    def test_read(self):
        rc, result, _ = self._run_json(["read", "a.md"])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertIn("hello world", result.get("content", ""))

    def test_search(self):
        rc, result, _ = self._run_json(["search", "hello"])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("hits") or result.get("results"))

    def test_write(self):
        rc, result, _ = self._run_json(["write", "b.md", "--content", "new note body"])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            (self.vault / "b.md").read_text(encoding="utf-8").strip(),
            "new note body",
        )

    def test_append(self):
        rc, result, _ = self._run_json(["append", "a.md", "appended line"])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertIn("appended line", (self.vault / "a.md").read_text(encoding="utf-8"))

    def test_patch(self):
        rc, result, _ = self._run_json(
            ["patch", "a.md", '[{"op":"set_field","field":"foo","value":42}]']
        )
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertIn("foo: 42", (self.vault / "a.md").read_text(encoding="utf-8"))

    def test_patch_items_batch(self):
        (self.vault / "c.md").write_text("---\nfoo: 9\n---\n\nbody\n", encoding="utf-8")
        items = json.dumps(
            [
                {"path": "a.md", "ops": [{"op": "set_field", "field": "foo", "value": 100}]},
                {"path": "c.md", "ops": [{"op": "set_field", "field": "foo", "value": 200}]},
            ]
        )
        rc, result, _ = self._run_json(["patch", "--items", items])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("applied_paths"), 2)
        self.assertIn("foo: 100", (self.vault / "a.md").read_text(encoding="utf-8"))
        self.assertIn("foo: 200", (self.vault / "c.md").read_text(encoding="utf-8"))

    def test_filter(self):
        rc, result, _ = self._run_json(["filter", "--where", '{"foo": 1}'])
        self.assertEqual(rc, 0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any(n.get("path") == "a.md" for n in result.get("notes", [])))

    # ---- validation rejection ---------------------------------------------

    def test_patch_rejects_unknown_op_with_friendly_message(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(["patch", "a.md", '[{"op":"not_a_real_op"}]'])
        msg = str(cm.exception)
        self.assertIn("error:", msg)
        # Same pydantic-driven hint path MCP gets via AgentValidationMiddleware —
        # not a raw traceback or an opaque ops.py KeyError.
        self.assertNotIn("Traceback", msg)
        # File unchanged — validation happens before any write.
        self.assertNotIn("not_a_real_op", (self.vault / "a.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
