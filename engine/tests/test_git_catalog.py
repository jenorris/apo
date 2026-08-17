"""Git ref catalog — filter_notes(ref=) / read_note(ref=)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apo_engine import config, core, git_catalog, ops

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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class GitCatalogOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.index = self.tmp / "index.db"
        _git(self.vault, "init", "-b", "main")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")

        policies = self.vault / "policies"
        policies.mkdir()
        (policies / "alpha.md").write_text(
            "---\ntitle: Alpha\nokf_type: policy\nstatus: draft\n---\n\n"
            "# Alpha\n\nmain body\n",
            encoding="utf-8",
        )
        (policies / "a — b.md").write_text(
            "---\ntitle: Em Dash\nokf_type: policy\nstatus: active\n---\n\n"
            "# Em Dash\n\nunicode path body\n",
            encoding="utf-8",
        )
        (self.vault / "catalog.yaml").write_text(
            "title: Catalog\nokf_type: concept\nstatus: active\n",
            encoding="utf-8",
        )
        schemas = self.vault / "system" / "contracts"
        schemas.mkdir(parents=True)
        (schemas / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: local\nhost: local\ndefault_branch: main\n",
            encoding="utf-8",
        )
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "main")

        _git(self.vault, "checkout", "-b", "feature/demo")
        (policies / "alpha.md").write_text(
            "---\ntitle: Alpha Feature\nokf_type: policy\nstatus: review\n---\n\n"
            "# Alpha\n\nfeature body\n\n## Details\n\nmore\n",
            encoding="utf-8",
        )
        (policies / "only-on-feature.md").write_text(
            "---\ntitle: Feature Only\nokf_type: policy\nstatus: active\n---\n\n"
            "# Feature Only\n\nbranch exclusive\n",
            encoding="utf-8",
        )
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "feature tip")
        _git(self.vault, "checkout", "main")

        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "git_catalog_test"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        git_catalog.reset_build_counts()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filter_notes_ref_diverges_from_main(self):
        main = ops.filter_notes({"okf_type": "policy"}, folder="policies", limit=20)
        self.assertTrue(main["ok"], main)
        main_paths = {n["path"] for n in main["notes"]}
        self.assertIn("policies/alpha.md", main_paths)
        self.assertNotIn("policies/only-on-feature.md", main_paths)

        feat = ops.filter_notes(
            {"okf_type": "policy"},
            folder="policies",
            limit=20,
            ref="feature/demo",
        )
        self.assertTrue(feat["ok"], feat)
        self.assertEqual(feat.get("source"), "git_ref")
        feat_paths = {n["path"] for n in feat["notes"]}
        self.assertIn("policies/only-on-feature.md", feat_paths)
        self.assertIn("policies/a — b.md", feat_paths)
        alpha_feat = next(n for n in feat["notes"] if n["path"] == "policies/alpha.md")
        self.assertEqual(alpha_feat["frontmatter"]["status"], "review")

    def test_filter_yaml_catalog_at_ref(self):
        out = ops.filter_notes({"okf_type": "concept"}, ref="feature/demo")
        self.assertTrue(out["ok"], out)
        paths = {n["path"] for n in out["notes"]}
        self.assertIn("catalog.yaml", paths)

    def test_read_note_at_ref_body_and_missing_on_wt(self):
        out = ops.read_note("policies/alpha.md", ref="feature/demo")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("source"), "git_ref")
        self.assertIn("feature body", out["content"])
        self.assertNotIn("mtime", out)
        self.assertIn("git_tip_mtime", out)
        self.assertNotIn("frontmatter_hash", out)
        self.assertNotIn("body_hash", out)
        self.assertNotIn("content_hash", out)

        missing_wt = self.vault / "policies" / "only-on-feature.md"
        self.assertFalse(missing_wt.exists())
        only = ops.read_note("policies/only-on-feature.md", ref="feature/demo")
        self.assertTrue(only["ok"], only)
        self.assertIn("branch exclusive", only["content"])

        uni = ops.read_note("policies/a — b.md", ref="feature/demo")
        self.assertTrue(uni["ok"], uni)
        self.assertIn("unicode path body", uni["content"])

    def test_read_note_toc_from_blob(self):
        out = ops.read_note("policies/alpha.md", ref="feature/demo", mode="toc")
        self.assertTrue(out["ok"], out)
        titles = [e["title"] for e in out["toc"]]
        self.assertIn("Alpha", titles)
        self.assertIn("Details", titles)

    def test_unknown_ref_and_chunk_hash_xor(self):
        bad = ops.filter_notes({}, ref="does-not-exist-xyz")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad.get("error"), "not_found")
        self.assertIn("list_refs", bad.get("message", ""))
        self.assertIn("feature/demo", bad.get("message", ""))
        xor = ops.read_note("policies/alpha.md", chunk_hash="deadbeef", ref="feature/demo")
        self.assertFalse(xor["ok"])
        self.assertEqual(xor.get("error"), "bad_request")

    def test_list_refs_heads_and_kind(self):
        out = ops.list_refs_op(kind="heads")
        self.assertTrue(out["ok"], out)
        names = {r["name"] for r in out["refs"]}
        self.assertIn("main", names)
        self.assertIn("feature/demo", names)
        self.assertTrue(all("commit" in r for r in out["refs"]))
        bad = ops.list_refs_op(kind="nope")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad.get("error"), "bad_request")

    def test_write_rejects_ref(self):
        out = ops.write_note("policies/x.md", content="# x\n", ref="feature/demo")
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("error"), "bad_request")
        self.assertFalse(ops.append_note("policies/alpha.md", text="x", ref="main")["ok"])
        self.assertFalse(
            ops.patch_note(
                "policies/alpha.md",
                [{"op": "set_field", "field": "status", "value": "x"}],
                ref="main",
            )["ok"]
        )
        self.assertFalse(ops.delete_note("policies/alpha.md", ref="main")["ok"])

    def test_ignore_contract_schemas(self):
        out = ops.filter_notes({}, ref="feature/demo", limit=100)
        self.assertTrue(out["ok"], out)
        paths = {n["path"] for n in out["notes"]}
        self.assertNotIn("system/contracts/git-contract.schema.yaml", paths)

    def test_cache_hit_skips_rebuild(self):
        git_catalog.reset_build_counts()
        a = ops.filter_notes({"okf_type": "policy"}, ref="feature/demo")
        self.assertTrue(a["ok"], a)
        tree = a["tree_oid"]
        self.assertEqual(git_catalog.build_count(tree), 1)
        b = ops.filter_notes({"status": "review"}, ref="feature/demo")
        self.assertTrue(b["ok"], b)
        self.assertEqual(git_catalog.build_count(tree), 1)

    def test_cache_rebuilds_when_ignore_changes(self):
        git_catalog.reset_build_counts()
        a = ops.filter_notes({"okf_type": "policy"}, ref="feature/demo")
        self.assertTrue(a["ok"], a)
        tree = a["tree_oid"]
        self.assertEqual(git_catalog.build_count(tree), 1)
        (self.vault / ".indexignore").write_text(
            "policies/only-on-feature.md\n", encoding="utf-8"
        )
        b = ops.filter_notes({"okf_type": "policy"}, ref="feature/demo", limit=50)
        self.assertTrue(b["ok"], b)
        self.assertEqual(git_catalog.build_count(tree), 2)
        paths = {n["path"] for n in b["notes"]}
        self.assertNotIn("policies/only-on-feature.md", paths)

    def test_ref_read_does_not_poison_touches(self):
        ops._recent_touches.clear()
        out = ops.read_note("policies/alpha.md", ref="feature/demo")
        self.assertTrue(out["ok"], out)
        self.assertEqual(ops._recent_touches, {})
        wt = ops.read_note("policies/alpha.md")
        self.assertTrue(wt["ok"], wt)
        self.assertTrue(ops._recent_touches)

    def test_read_blob_rejects_dotdot(self):
        tip = git_catalog.resolve_tree(self.vault, "feature/demo")
        with self.assertRaises(git_catalog.GitCatalogError) as ctx:
            git_catalog.read_blob(self.vault, tip.commit_oid, "../etc/passwd")
        self.assertEqual(ctx.exception.code, "bad_path")

    def test_batch_skips_oversized_blob_without_capture_output(self):
        """Large blobs are drained from the pipe; catalog still indexes peers."""
        big = self.vault / "policies" / "huge.md"
        body = "x" * (64 * 1024)
        big.write_text(
            "---\ntitle: Huge\nokf_type: policy\nstatus: draft\n---\n\n" + body,
            encoding="utf-8",
        )
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "huge")
        tip = git_catalog.resolve_tree(self.vault, "feature/demo")
        path_oids = git_catalog.list_blob_oids(
            self.vault,
            tip.tree_oid,
            ["policies/alpha.md", "policies/huge.md"],
        )
        with mock.patch.object(git_catalog, "_MAX_BLOB_BYTES", 1024):
            with mock.patch.object(git_catalog, "_run_git") as run_git:
                out = git_catalog._batch_read_blobs(self.vault, path_oids)
        self.assertIn("policies/alpha.md", out)
        self.assertNotIn("policies/huge.md", out)
        # Streaming path must not use capture_output batch via _run_git.
        for call in run_git.call_args_list:
            self.assertNotIn("--batch", call.args)

    def test_batch_raises_when_catalog_budget_exceeded(self):
        tip = git_catalog.resolve_tree(self.vault, "feature/demo")
        path_oids = git_catalog.list_blob_oids(
            self.vault,
            tip.tree_oid,
            ["policies/alpha.md", "catalog.yaml"],
        )
        with mock.patch.object(git_catalog, "_MAX_CATALOG_BYTES", 40):
            with self.assertRaises(git_catalog.GitCatalogError) as ctx:
                git_catalog._batch_read_blobs(self.vault, path_oids)
        self.assertIn("exceeds", ctx.exception.message)

    def test_read_blob_rejects_oversized(self):
        big = self.vault / "policies" / "toobig.md"
        big.write_text(
            "---\ntitle: Too Big\nokf_type: policy\n---\n\n" + ("y" * 2048),
            encoding="utf-8",
        )
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "toobig")
        commit = _git(self.vault, "rev-parse", "HEAD").stdout.strip()
        with mock.patch.object(git_catalog, "_MAX_BLOB_BYTES", 512):
            with self.assertRaises(git_catalog.GitCatalogError) as ctx:
                git_catalog.read_blob(self.vault, commit, "policies/toobig.md")
        self.assertIn("too large", ctx.exception.message)

    def test_git_dir_env_does_not_redirect(self):
        other = self.tmp / "other"
        other.mkdir()
        _git(other, "init", "-b", "main")
        _git(other, "config", "user.email", "test@example.com")
        _git(other, "config", "user.name", "Test")
        (other / "only-other.md").write_text(
            "---\ntitle: Other\nokf_type: policy\n---\n\n# Other\n",
            encoding="utf-8",
        )
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "other")

        old = os.environ.get("GIT_DIR")
        try:
            os.environ["GIT_DIR"] = str(other / ".git")
            out = ops.filter_notes({"okf_type": "policy"}, ref="feature/demo", limit=50)
            self.assertTrue(out["ok"], out)
            paths = {n["path"] for n in out["notes"]}
            self.assertIn("policies/alpha.md", paths)
            self.assertNotIn("only-other.md", paths)
        finally:
            if old is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = old


if __name__ == "__main__":
    unittest.main()
